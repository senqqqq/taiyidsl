"""Host declarations only: importing this module never imports ARTIQ."""

from pathlib import Path

from taiyidsl import us
from taiyidsl.native import NativeModule, NativeProgram, NativeStage

HERE = Path(__file__).resolve().parent


def build_native_program(*, readout_module=None, readout_phase_sensitive=True):
    """Four independent objects, with one owner of the shared preparation TTL."""

    def module(name, class_name, resources, **hooks):
        return NativeModule.from_package(
            root=HERE.parent,
            entry=f"native_stages.{name}:{class_name}",
            resources=resources,
            **hooks,
        )

    return NativeProgram(
        modules={
            "mot": module(
                "mot",
                "MOT",
                ("native_prepare",),
                initialize="initialize",
                after_shot="after_shot",
                finalize="finalize",
            ),
            "cooling": module("cooling", "Cooling", ("native_prepare",)),
            "state": module("state_prep", "StatePreparation", ("native_prepare",)),
            "readout": readout_module
            or module(
                "readout", "Readout", ("native_camera",), initialize="initialize"
            ),
        },
        stages={
            "prepare_MOT": NativeStage("mot", "prepare_MOT", timing="prefix"),
            "GM_cooling": NativeStage(
                "cooling",
                "GM_cooling",
                timing="timed",
                duration=20 * us,
                end_boundary="preparation TTL falling edge",
            ),
            "state_prep": NativeStage(
                "state",
                "state_prep",
                timing="timed",
                duration=8 * us,
                end_boundary="final preparation TTL falling edge",
            ),
            "readout": NativeStage(
                "readout",
                "execute_readout",
                timing="suffix",
                duration=10 * us,
                phase_sensitive=readout_phase_sensitive,
            ),
        },
        shared_resources={"native_prepare": ("mot", "cooling", "state")},
    )
