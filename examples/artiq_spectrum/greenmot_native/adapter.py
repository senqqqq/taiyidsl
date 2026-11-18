"""Reusable, offline GreenMOT declarations for the laboratory adapter layer.

Importing this module does not import ARTIQ or the external GreenMOT program.
NativeKernelPackage pins those sources; only the exported experiment invokes
its native class. Device names and exact preparation boundaries are deliberately
laboratory-specific and are not part of taiyidsl's generic operation library.
"""

import ast
from pathlib import Path

from taiyidsl import NativeKernelPackage, ms, ns

NATIVE = Path(__file__).resolve().parent
REVIEWED_COMMIT = "150c7fe724e8af078d97eeb8718f3e10d210d69c"
GREENMOT_RESOURCES = (
    "AOM_556_Lock",
    "AOM_399_Push",
    "AOM_578_Shift",
    "AOM_399_Probe",
    "AOM_532_Switch",
    "AOM_399_3DMOT",
    "AOM_399_2DMOT",
    "AOM_556_pump",
    "AOM_532_stabili",
    "AOM_399_zeeman",
    "AOM_556_Probe",
    "AOM_556_3DMOT",
    "AOM_556_Raman",
    "zotino0",
    "ttl_MindVision_Cam",
    "ttl_Camera_Trig",
    "ttl_399_Probe_Sh",
    "ttl_399_Push_Sh",
    "ttl_399_3DMOT_Sh",
    "ttl_556_3DMOT_Sh",
)


def _method_body(tree, class_name, method_name):
    classes = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    ]
    methods = (
        [
            n
            for n in classes[0].body
            if isinstance(n, ast.FunctionDef) and n.name == method_name
        ]
        if len(classes) == 1
        else []
    )
    if len(methods) != 1:
        raise ValueError(f"Expected exactly one {class_name}.{method_name}")
    body = methods[0].body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return body


def _ast_statements(body):
    return ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)


def validate_greenmot_split(source, adapter=None):
    """Prove the adapter split preserves the provided upstream preparation body.

    This reads ASTs, never imports or executes user code. Formatting and comments
    may differ; any operation/value/order change in the original method rejects.
    The original run's break+5 ms lead and the explicit final LDAC completion
    advance are checked separately and do not pretend to belong to the old body.
    """
    adapter = NATIVE / "greenmot_stages.py" if adapter is None else Path(adapter)
    original_tree = ast.parse(Path(source).read_text(encoding="utf-8-sig"))
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8-sig"))
    original = _method_body(original_tree, "_GreenMOTbase", "prepare_GreenMOT")
    prefix = _method_body(adapter_tree, "GreenMOTStages", "prepare_MOT")
    tail = _method_body(adapter_tree, "GreenMOTStages", "finish_preparation")
    legacy_lead = ast.parse("self.core.break_realtime()\ndelay(5 * ms)").body
    ldac_completion = ast.parse("delay_mu(2 * self.zotino0.bus.ref_period_mu)").body
    if (
        _ast_statements(prefix[:2]) != _ast_statements(legacy_lead)
        or _ast_statements(tail[-1:]) != _ast_statements(ldac_completion)
        or _ast_statements(prefix[2:] + tail[:-1]) != _ast_statements(original)
    ):
        raise ValueError(
            "GreenMOT preparation changed: review the native prefix/strict tail split"
        )


def native_package(greenmot_source=None):
    """Select source files explicitly; never silently fall back from actual code."""
    if greenmot_source is None:
        return NativeKernelPackage.from_package(
            root=NATIVE,
            entry="greenmot_fixture:GreenMOTFixture",
            resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
            initialize_method="initialize_native",
            finalize_method="finalize_native",
        )
    source = Path(greenmot_source).resolve(strict=True)
    validate_greenmot_split(source)
    sources = {"lib._GreenMOTbase": source}
    initializer = source.with_name("__init__.py")
    if initializer.exists():
        sources["lib"] = initializer
    return NativeKernelPackage.from_package(
        root=NATIVE,
        entry="greenmot_stages:GreenMOTStages",
        source_overrides=sources,
        resources=GREENMOT_RESOURCES,
        initialize_method="initialize_native",
        finalize_method="finalize_native",
    )


class GreenMOTAdapter:
    """Declare one native package and expose its prepared DSL-callable proxies.

    Construct once per experiment builder. ``prepare_MOT`` is an ordinary
    helper that appends two direct native calls, not a movable Section. The
    timed tail marks the actual preparation end before the caller's explicit
    gap. ``execute_readout`` starts at the compiled control body's end.
    """

    def __init__(self, greenmot_source=None):
        self.package = native_package(greenmot_source)
        self.prepare_prefix = self.package.kernel("prepare_MOT", timing="prefix")
        self.finish_preparation = self.package.kernel(
            "finish_preparation",
            timing="timed",
            duration=15 * ms + 16 * ns,
            end_boundary=(
                "Zotino LDAC final rising edge; analog settling not included"
                if greenmot_source
                else "Fixture TTL falling edge; no analog preparation model"
            ),
        )
        self.execute_readout = self.package.kernel(
            "execute_readout",
            timing="suffix",
            duration=100.48 * ms,
            phase_sensitive=False,
        )
        self.readout = self.execute_readout

    def prepare_MOT(self):
        """Append the original prefix and strict tail, without an extra scope."""
        self.prepare_prefix()
        self.finish_preparation()
