"""DSL-side declarations: pin kernel source without importing ARTIQ."""

from pathlib import Path

from taiyidsl import NativeModule, NativeProgram, NativeStage, us


def build_native_program():
    camera = NativeModule.from_package(
        root=Path(__file__).resolve().parents[1],
        entry="my_lab.native:CameraTrigger",
        resources=("camera_trigger",),
        initialize="initialize",
        finalize="finalize",
    )
    return NativeProgram(
        modules={"camera": camera},
        stages={
            "readout": NativeStage(
                "camera", "trigger", timing="suffix", duration=10 * us
            )
        },
    )
