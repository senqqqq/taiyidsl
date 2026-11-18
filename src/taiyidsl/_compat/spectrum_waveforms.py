"""Compatibility access to AWG programs and their independent validator."""

from importlib import import_module

_PUBLIC_NAMES = [
    "RENDER_BYTES_PER_SAMPLE_CHANNEL",
    "build_card_program",
    "iter_program_samples",
    "materialize_segment",
    "reconstruct_window",
    "render_work_budget",
    "validate_program",
]
__all__ = list(_PUBLIC_NAMES)


def __getattr__(name):
    module = (
        "validation_awg" if name in {"validate_program", "_expected_window"} else "awg"
    )
    return getattr(import_module("taiyidsl.backends.artiq_spectrum." + module), name)


def __dir__():
    program = import_module("taiyidsl.backends.artiq_spectrum.awg")
    return sorted(set(dir(program)) | {"validate_program", "_expected_window"})
