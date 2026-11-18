"""Host-only, replaceable four-component ARTIQ declarations for the RB example."""

from pathlib import Path

from taiyidsl import NativeModule

try:
    from examples.artiq_spectrum.native_stages.declarations import build_native_program
except ModuleNotFoundError as error:
    if error.name != "examples":
        raise
    from native_stages.declarations import build_native_program


def build_rb_native_program():
    """Reuse preparation components and supply a phase-insensitive joint readout.

    These TTL stages exercise native lifecycle/timing composition. None claims
    to prepare or detect a physical atomic state. The DSL camera trigger starts
    the acquisition; the subsequent suffix controls a separate readout TTL.
    """
    sources = Path(__file__).resolve().parent / "native_stages"
    readout = NativeModule(
        module="native_stages.joint_readout",
        class_name="JointReadout",
        sources={
            "native_stages": sources / "__init__.py",
            "native_stages.joint_readout": sources / "joint_readout.py",
        },
        resources=("native_readout",),
        initialize="initialize",
    )
    return build_native_program(readout_module=readout, readout_phase_sensitive=False)
