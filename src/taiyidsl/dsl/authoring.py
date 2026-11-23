"""Optional function-style experiment builders over the existing DSL context."""

from __future__ import annotations

import inspect
from functools import wraps

from ._context import _active, _builder, _source
from .experiment import Experiment


def experiment(*, qubits, name=None, parameters=None, metadata=None):
    """Decorate a synchronous DSL builder that returns no value.

    Each call creates and returns a fresh :class:`Experiment`. It only builds
    the existing IR: compilation, instrument preparation and execution remain
    explicit. Qubit membership is fixed when decorating; values, parameters and
    metadata are snapshotted when calling, just like ``Experiment(...)``.

    Use ordinary undecorated functions for reusable subroutines. A decorated
    experiment cannot run inside another active builder. Free context managers
    below also work inside existing ``exp.average(...)`` blocks.
    """
    members = tuple(qubits.values()) if hasattr(qubits, "values") else tuple(qubits)
    if name is not None and (not isinstance(name, str) or not name):
        raise ValueError("Experiment name must be a nonempty string")

    def decorate(builder):
        if not inspect.isfunction(builder):
            raise TypeError("experiment decorates a synchronous Python function")
        if (
            inspect.iscoroutinefunction(builder)
            or inspect.isgeneratorfunction(builder)
            or inspect.isasyncgenfunction(builder)
            or hasattr(builder, "artiq_embedded")
        ):
            raise TypeError(
                "experiment requires a synchronous DSL builder, not an async, "
                "generator or native ARTIQ kernel function"
            )

        @wraps(builder)
        def build(*args, **kwargs):
            if _active.get() is not None:
                raise ValueError(
                    "Cannot call a decorated experiment inside an active builder; "
                    "use an ordinary DSL helper function"
                )
            result = Experiment(
                builder.__name__ if name is None else name,
                members,
                metadata=metadata,
                parameters=parameters,
            )
            result.root.source = _source(skip=(__file__,))
            token = _active.set(result)
            try:
                value = builder(*args, **kwargs)
                if value is not None:
                    raise TypeError(
                        "A decorated experiment builder must return None; "
                        "the decorator returns the new Experiment"
                    )
            finally:
                _active.reset(token)
            return result

        return build

    return decorate


def average(shots, mode="cyclic", execution="host", *, scope="global"):
    """Use the existing average scope on the currently active experiment."""
    return _builder().average(shots, mode=mode, execution=execution, scope=scope)


def sweep(parameter, execution="host", *, chunks=None, chunk_size=None):
    """Sweep one parameter or pair a group of parameters by index on the host."""
    return _builder().sweep(
        parameter, execution=execution, chunks=chunks, chunk_size=chunk_size
    )


def realtime(timing="strict"):
    """Use the existing realtime scope without changing epochs or execution."""
    return _builder().realtime(timing=timing)


def triggered_sequence(*, group, trigger=None, phase_reference="sequence"):
    """Use the existing whole-program scope and optional configured trigger."""
    if trigger is None:
        return _builder().triggered_sequence(
            group=group, phase_reference=phase_reference
        )
    return _builder().triggered_sequence(
        group=group, trigger=trigger, phase_reference=phase_reference
    )


def repeat(count):
    """Use the existing finite static repeat scope."""
    return _builder().repeat(count)


def case(parameter, value):
    """Use the existing compile-time case selection scope."""
    return _builder().case(parameter, value)


def stage(name, *, parameters=None, exact_duration=None):
    """Use the optional named serial scope; no stage is inserted implicitly."""
    return _builder().stage(name, parameters=parameters, exact_duration=exact_duration)
