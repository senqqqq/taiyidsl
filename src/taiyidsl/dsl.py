"""A structural builder: executing these functions never accesses devices."""

from __future__ import annotations

import contextlib
import copy
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType

from .units import UNIT_TABLE, Quantity, as_fraction

_active = ContextVar("taiyidsl_active_experiment", default=None)


@dataclass(frozen=True)
class SweepParameter:
    name: str
    values: tuple
    unit: str = "s"

    def __post_init__(self):
        if not self.name or not self.name.isidentifier():
            raise ValueError("Sweep name must be a Python identifier")
        if self.unit not in UNIT_TABLE:
            raise ValueError(f"Unsupported unit: {self.unit}")
        unit = UNIT_TABLE[self.unit]
        values = tuple(
            as_fraction(v, unit.dimension)
            if isinstance(v, Quantity)
            else as_fraction(v, unit.dimension) * unit.value
            for v in self.values
        )
        if not values:
            raise ValueError("Sweep values cannot be empty")
        object.__setattr__(self, "values", values)

    @property
    def dimension(self):
        return UNIT_TABLE[self.unit].dimension


@dataclass(frozen=True)
class Square:
    duration: object
    amplitude: object = None
    phase: object = 0
    frequency: object = None


@dataclass
class Node:
    kind: str
    params: dict = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    source: str = ""


def _source():
    frame = inspect.currentframe()
    try:
        frame = frame.f_back
        while frame:
            if frame.f_code.co_filename not in {__file__, contextlib.__file__}:
                return f"{frame.f_code.co_filename}:{frame.f_lineno}"
            frame = frame.f_back
        return "unknown"
    finally:
        del frame


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


class Experiment:
    def __init__(
        self,
        name,
        qubits,
        metadata=None,
        *,
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
        self.metadata = copy.deepcopy(metadata or {})
        self.root = Node("experiment", source=_source())
        self._stack = [self.root]
        self._average_defined = False

    @contextmanager
    def _block(self, kind, **params):
        other = _active.get()
        if other is not None and other is not self:
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

    def average(self, shots, mode="cyclic", execution="host"):
        _positive_int(shots, "shots")
        if mode not in {"cyclic", "sequential"}:
            raise ValueError("mode must be cyclic or sequential")
        if execution != "host":
            raise NotImplementedError("v0.1 averaging supports host execution only")
        if self._average_defined or len(self._stack) != 1:
            raise ValueError("Exactly one top-level average block is supported")
        self._average_defined = True
        return self._block("average", shots=shots, mode=mode, execution=execution)

    def sweep(self, parameter, execution="host"):
        if execution != "host":
            raise NotImplementedError("v0.1 sweeps support host execution only")
        if not isinstance(parameter, SweepParameter):
            raise TypeError("sweep requires a SweepParameter")
        if any(n.kind == "realtime" for n in self._stack):
            raise ValueError("Host sweep cannot be inside realtime")
        return self._block("sweep", parameter=parameter)

    def realtime(self, timing="strict"):
        if timing != "strict":
            raise NotImplementedError(
                "v0.1 preserves exact timing; implicit padding is unsupported"
            )
        if any(n.kind == "realtime" for n in self._stack):
            raise ValueError("Realtime blocks cannot be nested")
        return self._block("realtime", timing=timing)

    def serial(self, exact_duration=None):
        return self._block("serial", exact_duration=exact_duration)

    def parallel(self, align="start", exact_duration=None):
        if align not in {"start", "end"}:
            raise ValueError("Parallel alignment must be start or end")
        return self._block("parallel", align=align, exact_duration=exact_duration)

    def repeat(self, count):
        _positive_int(count, "count")
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
        if len(self._stack) == 1:
            raise ValueError("Operations must be inside an averaging block")
        self._stack[-1].children.append(
            Node(kind, copy.deepcopy(params), source=_source())
        )


def _builder():
    exp = _active.get()
    if exp is None:
        raise RuntimeError("Operation requires an active Experiment block")
    return exp


def _signal(signal):
    if not isinstance(signal, str) or not signal:
        raise TypeError("Signal must be a nonempty logical signal name")
    return signal


def play(signal, pulse):
    if not isinstance(pulse, Square):
        raise NotImplementedError("v0.1 supports Square pulses only")
    _builder()._append("play", signal=_signal(signal), pulse=pulse)


def delay(signal, duration):
    _builder()._append("delay", signal=_signal(signal), duration=duration)


def reserve(resource, duration):
    """Reserve a logical signal or a named physical resource for this duration."""
    _builder()._append("reserve", signal=_signal(resource), duration=duration)


def shift_phase(frame, angle):
    _builder()._append("shift_phase", frame=_signal(frame), angle=angle)


def reset_frame(frame):
    _builder()._append("reset_frame", frame=_signal(frame))


def prepare(qubit, protocol="standard"):
    exp = _builder()
    if any(n.kind == "realtime" for n in exp._stack):
        raise ValueError("prepare must be outside realtime")
    exp._append("prepare", qubit=qubit.uid, protocol=protocol)


def measure(qubit, protocol="state_readout", handle="population"):
    if not isinstance(handle, str) or not handle:
        raise ValueError("Measurement handle must be nonempty")
    _builder()._append("measure", qubit=qubit.uid, protocol=protocol, handle=handle)


def serial(exact_duration=None):
    return _builder().serial(exact_duration)


def parallel(align="start", exact_duration=None):
    return _builder().parallel(align, exact_duration)


def strict_timing():
    return _builder().serial()


def resolve(value, coordinates, dimension):
    if isinstance(value, SweepParameter):
        if value.dimension != dimension:
            raise ValueError(
                f"Parameter {value.name} has dimension {value.dimension}, "
                f"expected {dimension}"
            )
        if value.name not in coordinates:
            raise ValueError(f"Unbound sweep parameter {value.name}")
        return coordinates[value.name]
    return as_fraction(value, dimension)


def json_value(value):
    """Portable representation for IR/provenance; never pickle live Python objects."""
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, Quantity):
        return {"value": float(value), "dimension": value.dimension}
    if isinstance(value, SweepParameter):
        return {
            "parameter": value.name,
            "values": [float(v) for v in value.values],
            "unit": value.unit,
        }
    if isinstance(value, Square):
        return {
            k: json_value(getattr(value, k))
            for k in ("duration", "amplitude", "phase", "frequency")
        }
    if isinstance(value, Node):
        return {
            "kind": value.kind,
            "params": json_value(value.params),
            "children": [json_value(n) for n in value.children],
            "source": value.source,
        }
    if isinstance(value, dict) or isinstance(value, MappingProxyType):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value
