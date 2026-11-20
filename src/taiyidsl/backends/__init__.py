"""Explicit backend registration with lazy loading of the ARTIQ reference target."""

from .base import Backend, BackendCapabilities

__all__ = ["Backend", "BackendCapabilities", "get_backend", "register_backend"]

_BACKENDS: dict[str, Backend] = {}


def _validate_backend(backend):
    if not isinstance(backend, Backend):
        raise TypeError("Backend must implement the Backend contract")
    if not isinstance(backend.name, str) or not backend.name.isidentifier():
        raise ValueError("Backend name must be an identifier")
    if not backend.version or not backend.target or not backend.phase_policy:
        raise ValueError("Backend version, target and phase policy are required")
    if not isinstance(backend.capabilities, BackendCapabilities):
        raise TypeError("Backend must declare BackendCapabilities")
    return backend


def register_backend(backend):
    """Register an explicit instance; never discover/import third-party plugins."""
    backend = _validate_backend(backend)
    if (
        backend.name in {"artiq", "artiq_spectrum", "spectrum"}
        or backend.name in _BACKENDS
    ):
        raise ValueError(f"Backend already registered: {backend.name}")
    _BACKENDS[backend.name] = backend


def get_backend(backend="artiq"):
    """Resolve a name or accept a backend instance without registration."""
    if not isinstance(backend, str):
        return _validate_backend(backend)
    if backend == "artiq":
        from taiyidsl.backends.artiq.backend import ArtiqBackend

        return ArtiqBackend()
    if backend == "artiq_spectrum":
        from taiyidsl.backends.artiq_spectrum.backend import ArtiqSpectrumBackend

        return ArtiqSpectrumBackend()
    if backend == "spectrum":
        from taiyidsl.backends.spectrum.backend import SpectrumBackend

        return SpectrumBackend()
    if backend not in _BACKENDS:
        raise NotImplementedError(f"Unknown backend: {backend}")
    return _validate_backend(_BACKENDS[backend])
