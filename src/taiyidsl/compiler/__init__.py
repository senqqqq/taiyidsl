"""Offline compiler entry points; internal modules own each compilation stage.

Imports stay lazy so a backend can use scheduling without loading the pipeline.
"""

from importlib import import_module

__all__ = [
    "CompiledExperiment",
    "compile_experiment",
    "CompileError",
    "CompiledPoint",
    "ScheduledEvent",
    "acquisition_targets",
    "CompilationStore",
    "compile_stream",
]

_EXPORTS = {
    "CompiledExperiment": (".artifact", "CompiledExperiment"),
    "compile_experiment": (".pipeline", "compile_experiment"),
    "CompileError": ("..ir", "CompileError"),
    "CompiledPoint": ("..ir", "CompiledPoint"),
    "ScheduledEvent": ("..ir", "ScheduledEvent"),
    "acquisition_targets": ("..ir", "acquisition_targets"),
    "CompilationStore": (".store", "CompilationStore"),
    "compile_stream": (".streaming", "compile_stream"),
    "_walk": (".preparation", "_walk"),
    "_parameter_references": (".preparation", "_parameter_references"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | _EXPORTS.keys())
