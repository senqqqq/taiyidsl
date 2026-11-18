"""Compatibility exports for the canonical ARTIQ implementation package.

New library code imports the owning module under taiyidsl.backends.artiq.
"""

from importlib import import_module

_OWNERS = {
    "_KINDS": "taiyidsl.backends.artiq.polar_contracts",
    "uses_polar": "taiyidsl.backends.artiq.polar_contracts",
    "_parameters": "taiyidsl.backends.artiq.polar_contracts",
    "polar_bank_summary": "taiyidsl.backends.artiq.polar_contracts",
    "_signed": "taiyidsl.backends.artiq.polar_contracts",
    "_call": "taiyidsl.backends.artiq.codegen_polar",
    "_instruction": "taiyidsl.backends.artiq.codegen_polar",
    "generate_polar": "taiyidsl.backends.artiq.codegen_polar",
    "_target_lines": "taiyidsl.backends.artiq.codegen_polar",
    "_fail": "taiyidsl.backends.artiq.verification_polar",
    "_PolarSwitch": "taiyidsl.backends.artiq.verification_polar",
    "_PolarUpdate": "taiyidsl.backends.artiq.verification_polar",
    "_PolarProfile": "taiyidsl.backends.artiq.verification_polar",
    "_PolarDDS": "taiyidsl.backends.artiq.verification_polar",
    "_fake": "taiyidsl.backends.artiq.verification_polar",
    "verify_polar": "taiyidsl.backends.artiq.verification_polar",
    "_other_point": "taiyidsl.backends.artiq.verification_polar",
    "_expected_mixed_events": "taiyidsl.backends.artiq.verification_polar",
    "_verify_other_paths": "taiyidsl.backends.artiq.verification_polar",
    "_verify_state": "taiyidsl.backends.artiq.verification_polar",
    "ram_preload_lines": "taiyidsl.backends.artiq.codegen_ram",
    "square_effect_record": "taiyidsl.backends.artiq._contracts",
    "square_preload_lines": "taiyidsl.backends.artiq.codegen_square",
    "host_loop_lines": "taiyidsl.compiler.chunking",
    "device_execution_plan": "taiyidsl.execution",
    "execution_plan_for": "taiyidsl.host_plan",
    "validate_channel_order": "taiyidsl.backends.artiq.submission",
    "validate_sed": "taiyidsl.backends.artiq.submission",
    "validate_submission_plan": "taiyidsl.backends.artiq.submission",
}
_PUBLIC_NAMES = [
    "device_execution_plan",
    "execution_plan_for",
    "generate_polar",
    "host_loop_lines",
    "polar_bank_summary",
    "ram_preload_lines",
    "square_effect_record",
    "square_preload_lines",
    "uses_polar",
    "validate_channel_order",
    "validate_sed",
    "validate_submission_plan",
    "verify_polar",
]
__all__ = list(_PUBLIC_NAMES)


def __getattr__(name):
    try:
        owner = _OWNERS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(import_module(owner), name)


def __dir__():
    return sorted(set(globals()) | set(_OWNERS))
