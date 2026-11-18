"""Experiment snapshots, structural scopes and transactional block building."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from types import MappingProxyType

from ..config.parameters import ParameterSnapshot
from ..units import UNIT_TABLE, Quantity, as_fraction
from ._context import (
    _active,
    _has_scope,
    _in_section,
    _reject_section_boundary,
    _source,
)
from ._serialization import json_value
from .nodes import Node
from .values import SweepParameter, _positive_int, _signal, validate_count


class Experiment:
    def __init__(
        self,
        name,
        qubits,
        metadata=None,
        *,
        parameters=None,
        repetition_period=None,
        cross_shot_phase=False,
    ):
        if repetition_period is not None or cross_shot_phase:
            raise NotImplementedError(
                "Fixed host shot period and cross-shot phase continuity are unsupported"
            )
        if not isinstance(name, str) or not name:
            raise ValueError("Experiment name is required")
        items = list(qubits.values()) if hasattr(qubits, "values") else list(qubits)
        if len({q.uid for q in items}) != len(items):
            raise ValueError("Qubit identifiers must be unique")
        self.name = name
        self.qubits = MappingProxyType({q.uid: q.snapshot() for q in items})
        self._parameters = (
            parameters.snapshot()
            if isinstance(parameters, ParameterSnapshot)
            else ParameterSnapshot({} if parameters is None else parameters)
        )
        self.metadata = copy.deepcopy(metadata or {})
        self.root = Node("experiment", source=_source())
        self._stack = [self.root]
        self._average_defined = False

    @property
    def parameters(self):
        """The immutable parameter table captured when this experiment was built."""
        return self._parameters

    @contextmanager
    def _block(self, kind, **params):
        other = _active.get()
        if other is not None and other is not self:
            if getattr(other, "_owner", None) is self:
                with other._block(kind, **params) as node:
                    yield node
                return
            raise ValueError("Cannot nest builders from different experiments")
        node = Node(kind, params, source=_source())
        self._stack[-1].children.append(node)
        self._stack.append(node)
        token = _active.set(self)
        try:
            yield node
        except BaseException:
            self._stack[-2].children.remove(node)
            if kind == "average":
                self._average_defined = False
            raise
        finally:
            self._stack.pop()
            _active.reset(token)

    def average(self, shots, mode="cyclic", execution="host", *, scope="global"):
        _reject_section_boundary("average")
        _positive_int(shots, "shots")
        if mode not in {"cyclic", "sequential"}:
            raise ValueError("mode must be cyclic or sequential")
        if execution != "host":
            raise NotImplementedError("v0.1 averaging supports host execution only")
        if scope not in {"global", "chunk"}:
            raise ValueError("average scope must be global or chunk")
        if self._average_defined or len(self._stack) != 1:
            raise ValueError("Exactly one top-level average block is supported")
        self._average_defined = True
        return self._block(
            "average", shots=shots, mode=mode, execution=execution, scope=scope
        )

    @contextmanager
    def sweep(self, parameter, execution="host", *, chunks=None, chunk_size=None):
        _reject_section_boundary("sweep")
        if execution != "host":
            raise NotImplementedError("v0.1 sweeps support host execution only")
        if not isinstance(parameter, SweepParameter):
            raise TypeError("sweep requires a SweepParameter")
        if any(n.kind == "realtime" for n in self._stack):
            raise ValueError("Host sweep cannot be inside realtime")
        if chunks is not None and chunk_size is not None:
            raise ValueError("chunks and chunk_size are mutually exclusive")
        options = {}
        for name, value in (("chunks", chunks), ("chunk_size", chunk_size)):
            if value is not None:
                if not (name == "chunks" and value == "auto"):
                    _positive_int(value, name)
                options[name] = value
        with self._block("sweep", parameter=parameter, **options):
            yield parameter

    def realtime(self, timing="strict"):
        """Build a strict shot body; children run serially unless grouped otherwise.

        Different signal names do not imply concurrent execution. Use parallel
        branches for overlap; insufficient hardware preparation time is an error.
        Python helpers execute while building the experiment and must emit DSL
        nodes. This scope does not capture arbitrary ARTIQ kernels, RPCs, host
        callbacks or Python wall-clock delays. It establishes neither a new
        shot epoch nor a new frame, and is not itself a kernel boundary.
        """
        _reject_section_boundary("realtime")
        if timing != "strict":
            raise NotImplementedError(
                "v0.1 preserves exact timing; implicit padding is unsupported"
            )
        if any(n.kind == "realtime" for n in self._stack):
            raise ValueError("Realtime blocks cannot be nested")
        return self._block("realtime", timing=timing)

    @contextmanager
    def stage(self, name, *, parameters=None, exact_duration=None):
        """Name a reusable serial DSL operation inside the realtime scope.

        This is an annotated serial block, not an adapter for native ARTIQ code.
        Parameters are provenance metadata; emitted DSL nodes define all timing
        and resources. ``exact_duration`` asserts the duration without padding.
        A complete stage is one branch when composed inside ``parallel``.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("stage requires a nonempty name")
        if not _in_section() and not _has_scope(self, "realtime"):
            raise ValueError("stage must be inside realtime")
        if isinstance(parameters, ParameterSnapshot):
            parameters = parameters.to_dict()
        values = json_value({} if parameters is None else parameters)
        try:
            json.dumps(values, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("stage parameters must be portable metadata") from exc
        with self.serial(exact_duration=exact_duration) as node:
            node.params["operation_name"] = name
            node.params["effective_parameters"] = values
            yield node

    def serial(self, exact_duration=None):
        """Run child operations in order, across all their signals.

        Group a reusable multi-step operation here to make it one parallel branch.
        ``exact_duration`` asserts the duration; it does not insert padding.
        """
        return self._block("serial", exact_duration=exact_duration)

    def triggered_sequence(self, *, group, trigger=None, phase_reference="sequence"):
        """Declare one complete, preloaded Spectrum program inside realtime.

        Internal gate calls and waits belong to the card program, not to
        individual ARTIQ triggers. Serial/parallel retain their normal meaning.
        The carrier starts from zero at this sequence's entry by default.
        Use phase_reference="shot" to retain the shot-wide time reference.
        Explicit frame updates before entry are preserved in either mode.
        """
        _reject_section_boundary("triggered_sequence")
        if not isinstance(group, str) or not group:
            raise TypeError("Spectrum group must be a nonempty name")
        if phase_reference not in {"sequence", "shot"}:
            raise ValueError("phase_reference must be 'sequence' or 'shot'")
        if not any(node.kind == "realtime" for node in self._stack):
            raise ValueError("triggered_sequence must be inside realtime")
        if any(node.kind == "triggered_sequence" for node in self._stack):
            raise ValueError("triggered_sequence blocks cannot be nested")
        if trigger is not None:
            _signal(trigger)
            return self._block(
                "triggered_sequence",
                group=group,
                trigger=trigger,
                phase_reference=phase_reference,
            )
        return self._block(
            "triggered_sequence", group=group, phase_reference=phase_reference
        )

    def parallel(self, align="start", exact_duration=None):
        """Align direct child branches and join at the longest branch's end.

        Use a serial block for successive operations in one branch. Overlapping
        use of one physical output is rejected, not automatically queued.
        """
        if align not in {"start", "end"}:
            raise ValueError("Parallel alignment must be start or end")
        return self._block("parallel", align=align, exact_duration=exact_duration)

    def repeat(self, count):
        validate_count(count)
        return self._block("repeat", count=count)

    def case(self, parameter, value):
        """Compile-time selection of a static sweep point, never realtime branching."""
        if not isinstance(parameter, SweepParameter):
            raise TypeError("case requires a SweepParameter")
        unit = UNIT_TABLE[parameter.unit]
        v = as_fraction(value, unit.dimension)
        if not isinstance(value, Quantity):
            v *= unit.value
        return self._block("case", parameter=parameter, value=v)

    def _append(self, kind, **params):
        other = _active.get()
        if other is not None and other is not self:
            if getattr(other, "_owner", None) is self:
                other._append(kind, **params)
                return
            raise ValueError("Cannot mix builders from different experiments")
        if len(self._stack) == 1:
            raise ValueError("Operations must be inside an averaging block")
        self._stack[-1].children.append(
            Node(kind, copy.deepcopy(params), source=_source())
        )

    def add(self, section):
        """Insert an independent snapshot of a completed Section here."""
        from .sections import _add_to

        other = _active.get()
        if other is not self and getattr(other, "_owner", None) is not self:
            raise ValueError("add requires this Experiment's active block")
        _add_to(other, section)
