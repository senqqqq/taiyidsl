"""Compatibility exports for the canonical ARTIQ implementation package.

New library code imports the owning module under taiyidsl.backends.artiq.
"""

from importlib import import_module

_OWNERS = {
    "ExportError": "taiyidsl.backends.artiq._contracts",
    "_devices": "taiyidsl.backends.artiq._contracts",
    "_origin_grid": "taiyidsl.backends.artiq._contracts",
    "_words": "taiyidsl.backends.artiq._contracts",
    "_ordered_events": "taiyidsl.backends.artiq._contracts",
    "_point_end": "taiyidsl.backends.artiq._contracts",
    "_validate_plan": "taiyidsl.backends.artiq.codegen",
    "_alignment": "taiyidsl.backends.artiq.codegen",
    "_program_groups": "taiyidsl.backends.artiq.codegen",
    "_kernel_header": "taiyidsl.backends.artiq.codegen",
    "_event_lines": "taiyidsl.backends.artiq.codegen",
    "_parameterized_kernel": "taiyidsl.backends.artiq.codegen",
    "_generate": "taiyidsl.backends.artiq.codegen",
    "generate_source": "taiyidsl.backends.artiq.codegen",
    "_DriverClock": "taiyidsl.backends.artiq.mock_drivers",
    "_MockCore": "taiyidsl.backends.artiq.mock_drivers",
    "_MockSwitch": "taiyidsl.backends.artiq.mock_drivers",
    "_MockDDS": "taiyidsl.backends.artiq.mock_drivers",
    "_MockRAMProfile": "taiyidsl.backends.artiq.mock_drivers",
    "_MockRAMUpdate": "taiyidsl.backends.artiq.mock_drivers",
    "_fake_artiq": "taiyidsl.backends.artiq.mock_drivers",
    "_expected_events": "taiyidsl.backends.artiq.verification_common",
    "_verify_ram_effects": "taiyidsl.backends.artiq.verification_common",
    "_verify_host_order": "taiyidsl.backends.artiq.verification_common",
    "_verify_source": "taiyidsl.backends.artiq.verification",
    "verify_export": "taiyidsl.backends.artiq.verification",
    "verify_equivalence": "taiyidsl.backends.artiq.verification",
    "export_artiq": "taiyidsl.backends.artiq.export",
    "ram_bank_summary": "taiyidsl.backends.artiq.codegen_ram",
    "ram_preload_lines": "taiyidsl.backends.artiq.codegen_ram",
    "ram_rearm_lines": "taiyidsl.backends.artiq.codegen_ram",
    "ram_table_body_lines": "taiyidsl.backends.artiq.codegen_ram",
    "ram_table_build_lines": "taiyidsl.backends.artiq.codegen_ram",
    "nco_release_stage_lines": "taiyidsl.backends.artiq.codegen_square",
    "square_effect_record": "taiyidsl.backends.artiq._contracts",
    "square_preload_lines": "taiyidsl.backends.artiq.codegen_square",
    "host_loop_lines": "taiyidsl.compiler.chunking",
    "device_execution_plan": "taiyidsl.execution",
    "execution_order": "taiyidsl.host_plan",
    "execution_plan_for": "taiyidsl.host_plan",
    "CompileError": "taiyidsl.ir",
    "_jsonable": "taiyidsl.serialization",
    "validate_channel_order": "taiyidsl.backends.artiq.submission",
    "validate_sed": "taiyidsl.backends.artiq.submission",
    "validate_submission_plan": "taiyidsl.backends.artiq.submission",
}
_PUBLIC_NAMES = [
    "CompileError",
    "ExportError",
    "device_execution_plan",
    "execution_order",
    "execution_plan_for",
    "export_artiq",
    "generate_source",
    "host_loop_lines",
    "nco_release_stage_lines",
    "ram_bank_summary",
    "ram_preload_lines",
    "ram_rearm_lines",
    "ram_table_body_lines",
    "ram_table_build_lines",
    "square_effect_record",
    "square_preload_lines",
    "validate_channel_order",
    "validate_sed",
    "validate_submission_plan",
    "verify_equivalence",
    "verify_export",
]
__all__ = list(_PUBLIC_NAMES)
_ALIASES = {"_jsonable": "to_jsonable"}


def __getattr__(name):
    try:
        owner = _OWNERS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(import_module(owner), _ALIASES.get(name, name))


def __dir__():
    return sorted(set(globals()) | set(_OWNERS))
