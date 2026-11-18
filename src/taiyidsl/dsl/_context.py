"""Shared construction context and caller provenance for the structural DSL."""

import contextlib
import inspect
from contextvars import ContextVar
from pathlib import Path

_active = ContextVar("taiyidsl_active_experiment", default=None)
# These files replace the original monolithic DSL implementation frames.
# User helpers and operation bodies remain meaningful provenance locations.
_SOURCE_FILES = {
    __file__,
    contextlib.__file__,
    str(Path(__file__).with_name("experiment.py")),
    str(Path(__file__).with_name("instructions.py")),
}


def _source(*, skip=()):
    frame = inspect.currentframe()
    try:
        frame = frame.f_back
        while frame:
            if frame.f_code.co_filename not in _SOURCE_FILES | set(skip):
                return f"{frame.f_code.co_filename}:{frame.f_lineno}"
            frame = frame.f_back
        return "unknown"
    finally:
        del frame


def _builder():
    exp = _active.get()
    if exp is None:
        raise RuntimeError("Operation requires an active Experiment block")
    return exp


def _in_section():
    return getattr(_active.get(), "_is_section_collector", False)


def _has_scope(builder, kind):
    return any(node.kind == kind for node in builder._stack) or kind in getattr(
        builder, "_ambient_scopes", ()
    )


def _reject_section_boundary(kind):
    if _in_section():
        raise ValueError(f"{kind} cannot be inside a reusable Section")
