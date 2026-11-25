"""Test-only child of the Nix launcher. No physical core or SDK fallback."""

import importlib.util
import os
import runpy
import socket
import sys
from pathlib import Path
from types import SimpleNamespace


def blocked(*args, **kwargs):
    raise AssertionError("Network/driver access is forbidden in the worker double")


def main():
    assert sys.argv[1:] == [
        "-P",
        "-u",
        "-m",
        "taiyidsl.runtime.worker_entry",
        "serve",
        "--spectrum",
    ]
    socket.socket.connect = socket.socket.connect_ex = blocked
    from artiq.master import worker_db

    from taiyidsl.runtime.worker_entry import main as entry

    case = os.environ["TAIYIDSL_TEST_CASE"]
    events = Path(os.environ["TAIYIDSL_TEST_EVENTS"])

    def record(event):
        with events.open("a", encoding="utf-8") as stream:
            print(event, file=stream)

    sdk = runpy.run_path(str(Path(__file__).with_name("spectrum_sdk_double.py")))[
        "FakeSdk"
    ]()
    sdk.__spec__ = importlib.util.find_spec("spcm")
    core_sdk = SimpleNamespace(
        __spec__=importlib.util.find_spec("spcm_core"), spcm_hOpen=blocked
    )
    sys.modules.update(spcm=sdk, spcm_core=core_sdk)

    class Log(list):
        def append(self, item):
            record(item[0])
            super().append(item)
            if item[0] == "dma" and case in {"partial_upload", "bench_partial_upload"}:
                raise RuntimeError("injected DMA failure")

    sdk.log = Log()
    if case.startswith("bench") and case != "bench_wrong_source":
        sdk.clock.update(mode=sdk.SPC_CM_INTPLL, reference_clock=10_000_000)
    if case == "not_ready":
        sdk.card.status = lambda: sdk.M2STAT_CARD_READY

    class Core:
        ref_period = 1e-9
        ref_multiplier = 8
        comm = SimpleNamespace(check_system_info=lambda: record("probe core"))

        def run(self, method, args, kwargs):
            # Boundary of kernel submission. Compilation is checked separately.
            record(method.__name__)
            if case == "bench_drift" and method.__name__ == "initialize_devices":
                sdk.clock["sample_rate"] //= 2
            if case == "disconnect" and method.__name__ == "point_0":
                os._exit(74)

    class Devices:
        def __init__(self, db):
            assert db.get("core")["arguments"]["host"] == "unit-test.invalid"
            os.write(1, b"SDK/native output belongs in worker.log\n")

        def get(self, name):
            return {"core": Core(), "ttl20": object()}[name]

        def close_devices(self):
            record("closed core")

    worker_db.DeviceManager = Devices
    sys.argv = [sys.argv[0], "serve", "--spectrum"]
    entry()


if __name__ == "__main__":
    main()
