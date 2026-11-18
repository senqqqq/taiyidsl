"""Reusable, unscheduled DSL subtrees with isolated construction and insertion.

Build a Section with ``with section(...) as segment``. With an active parent,
the completed segment is inserted once; without a parent it remains detached.
``add(segment)`` always inserts another independent snapshot. No Section creates
an experiment, averaging loop, timing epoch or hardware execution boundary.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass

from ._context import _active, _builder, _has_scope, _source
from ._serialization import json_value
from .experiment import Experiment
from .nodes import Node

_SCOPES = {"serial", "parallel", "repeat", "case"}
_OPERATIONS = {
    "move",
    "play",
    "trigplay",
    "trig",
    "delay",
    "section_delay",
    "reserve",
    "shift_phase",
    "reset_frame",
    "reset_phase",
    "rz",
    "measure",
    "acquire",
    "barrier",
}


@dataclass(frozen=True, init=False)
class Section:
    """A completed DSL template, bound to its original qubits and parameters.

    Use :func:`section` or :func:`qubit_operation` to construct one. Public
    structural accessors return copies. A handle yielded during construction
    cannot be inspected or inserted until its context exits successfully.
    """

    _node: Node | None
    _target_snapshots: tuple

    def __init__(self):
        raise TypeError("Build a Section with section() or @qubit_operation")

    @classmethod
    def _pending(cls):
        value = object.__new__(cls)
        object.__setattr__(value, "_node", None)
        object.__setattr__(value, "_target_snapshots", ())
        return value

    @property
    def finalized(self):
        """Whether construction completed successfully."""
        return self._node is not None

    def _require_finalized(self):
        if not self.finalized:
            raise ValueError("Section construction has not completed successfully")

    @property
    def name(self):
        self._require_finalized()
        return self._node.params.get("operation_name")

    @property
    def schedule(self):
        self._require_finalized()
        if self._node.params.get("circuit_layer"):
            return "parallel"
        return self._node.params["schedule"]

    @property
    def align(self):
        """Alignment of parallel branches, or None for serial/ASAP Sections."""
        self._require_finalized()
        if self._node.params.get("circuit_layer"):
            return self._node.children[0].params["align"]
        return None

    @property
    def qubits(self):
        """Bound qubit identifiers, including nested operation dependencies."""
        self._require_finalized()
        return tuple(value["uid"] for value in self._target_snapshots)

    @property
    def parameters(self):
        """A copy of the recorded operation arguments."""
        self._require_finalized()
        return copy.deepcopy(self._node.params.get("effective_parameters", {}))

    def to_node(self):
        """Return an independent copy of the unscheduled internal Node tree."""
        self._require_finalized()
        return copy.deepcopy(self._node)

    def to_dict(self):
        """Return portable structural data, without resolving sweep values."""
        return json_value(self.to_node())


def _owner(builder):
    return (
        builder if isinstance(builder, Experiment) else getattr(builder, "_owner", None)
    )


def _validate_parent(builder):
    if builder is not None and not (
        getattr(builder, "_is_section_collector", False)
        or _has_scope(builder, "realtime")
    ):
        raise ValueError(
            "A Section must be inserted inside realtime or another Section"
        )


def _validate_targets(experiment, snapshots):
    if experiment is None:
        return
    for snapshot in snapshots:
        uid = snapshot["uid"]
        target = experiment.qubits.get(uid)
        if target is None:
            raise ValueError(
                f"Section target qubit {uid} is absent from the Experiment"
            )
        if target.to_dict() != snapshot:
            raise ValueError(
                f"Section target qubit {uid} has a different parameter snapshot; "
                "rebuild the operation with the Experiment's qubit"
            )


class _SectionCollector:
    """Only structural DSL collection; deliberately not an Experiment subclass."""

    _is_section_collector = True

    def __init__(self, parent, root, targets):
        self._owner = _owner(parent)
        self._ambient_scopes = (
            tuple(node.kind for node in parent._stack)
            + getattr(parent, "_ambient_scopes", ())
            if parent is not None
            else ()
        )
        self.root = root
        self._stack = [root]
        self._targets = {}
        self._merge_targets(target.to_dict() for target in targets)

    def _merge_targets(self, snapshots):
        incoming = {}
        for snapshot in snapshots:
            uid = snapshot["uid"]
            existing = incoming.get(uid, self._targets.get(uid))
            if existing is not None and existing != snapshot:
                raise ValueError(f"Conflicting parameter snapshots for qubit {uid}")
            incoming[uid] = copy.deepcopy(snapshot)
        _validate_targets(self._owner, incoming.values())
        self._targets.update(incoming)

    @contextmanager
    def _block(self, kind, **params):
        if kind not in _SCOPES:
            raise ValueError(f"{kind} cannot be inside a reusable Section")
        if _active.get() is not self:
            raise ValueError("Cannot write a Section through an inactive builder")
        node = Node(kind, copy.deepcopy(params), source=_source(skip=(__file__,)))
        parent = self._stack[-1]
        targets = copy.deepcopy(self._targets)
        parent.children.append(node)
        self._stack.append(node)
        try:
            yield node
        except BaseException:
            parent.children.remove(node)
            self._targets = targets
            raise
        finally:
            self._stack.pop()

    def _append(self, kind, **params):
        if kind not in _OPERATIONS:
            raise ValueError(f"{kind} cannot be inside a reusable Section")
        if _active.get() is not self:
            raise ValueError("Cannot write a Section through an inactive builder")
        self._stack[-1].children.append(
            Node(kind, copy.deepcopy(params), source=_source(skip=(__file__,)))
        )

    # Reuse the established structural validation and Node shapes. Host/epoch
    # methods reject through _reject_section_boundary before touching host state.
    serial = Experiment.serial
    parallel = Experiment.parallel
    repeat = Experiment.repeat
    case = Experiment.case
    stage = Experiment.stage
    average = Experiment.average
    sweep = Experiment.sweep
    realtime = Experiment.realtime
    triggered_sequence = Experiment.triggered_sequence

    def add(self, segment):
        _add_to(self, segment)


@contextmanager
def _capture_section(
    name, schedule, *, targets=(), parameters=None, source, layer_align=None
):
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("Section name must be a nonempty string or None")
    if schedule not in {"serial", "asap"}:
        raise ValueError("Section schedule must be serial or asap")
    parent = _active.get()
    _validate_parent(parent)
    root = Node(
        "serial",
        {
            "schedule": schedule,
            "operation_name": name,
            "operation_qubits": list(dict.fromkeys(target.uid for target in targets)),
            "effective_parameters": copy.deepcopy(parameters or {}),
        },
        source=source,
    )
    if layer_align is not None:
        root.params["circuit_layer"] = True
    collector = _SectionCollector(parent, root, targets)
    result = Section._pending()
    token = _active.set(collector)
    try:
        yield result
        if len(collector._stack) != 1:
            raise ValueError("Cannot finish a Section while a builder block is open")
    finally:
        _active.reset(token)
    object.__setattr__(result, "_node", copy.deepcopy(root))
    object.__setattr__(
        result, "_target_snapshots", tuple(copy.deepcopy(collector._targets).values())
    )
    if parent is not None:
        _add_to(parent, result)


@contextmanager
def section(name=None, *, schedule="serial", align=None, qubits=()):
    """Capture one reusable serial, ASAP or aligned parallel experiment segment.

    A successful context inside an active experiment/Section inserts one copy.
    The same context outside a builder produces a detached template. Entering
    and leaving the context does not reset phase or start a hardware sequence.

    Parallel Sections declare their participating qubits. Each direct child is
    an indivisible branch; wrap a multi-operation branch in a serial Section.
    They form full boundaries even inside an ASAP parent, as legacy layers do.
    """
    from ..config.models import Qubit

    qubits = tuple(qubits)
    if any(not isinstance(q, Qubit) for q in qubits):
        raise TypeError("section qubits must be Qubit targets")
    if len({q.uid for q in qubits}) != len(qubits):
        raise ValueError("section qubit targets must be unique")
    if schedule not in {"serial", "asap", "parallel"}:
        raise ValueError("Section schedule must be serial, asap or parallel")
    if schedule != "parallel":
        if align is not None:
            raise ValueError("align is only defined for a parallel Section")
        with _capture_section(
            name, schedule, targets=qubits, source=_source(skip=(__file__,))
        ) as result:
            yield result
        return
    if not qubits:
        raise TypeError("A parallel Section requires one or more Qubit targets")
    align = "start" if align is None else align
    if align not in {"start", "end"}:
        raise ValueError("Section align must be start or end")
    with _capture_section(
        name,
        "serial",
        targets=qubits,
        source=_source(skip=(__file__,)),
        layer_align=align,
    ) as result:
        with _builder()._block(
            "parallel", align=align, layer_qubits=[q.uid for q in qubits]
        ):
            yield result


@contextmanager
def layer(*qubits, name="layer", align="start"):
    """Compatibility spelling for an aligned parallel :func:`section`."""
    with section(name, schedule="parallel", align=align, qubits=qubits) as result:
        yield result


def _add_to(builder, segment):
    if not isinstance(segment, Section):
        raise TypeError("add requires a Section")
    segment._require_finalized()
    _validate_parent(builder)
    _validate_targets(_owner(builder), segment._target_snapshots)
    if getattr(builder, "_is_section_collector", False):
        builder._merge_targets(segment._target_snapshots)
    builder._stack[-1].children.append(segment.to_node())
    # A composite adapter may return the same Section it just inserted. Track
    # identity only while that adapter asks; ordinary builders retain no list.
    insertions = getattr(builder, "_section_insertions", None)
    if insertions is not None:
        insertions.append(segment)


def add(segment):
    """Insert another snapshot of a completed Section in the current context.

    An operation called in an active context already inserts itself. Use either
    that direct call or ``add`` on a previously built template, not both for one
    intended occurrence.
    """
    _add_to(_builder(), segment)
