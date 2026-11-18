"""Owned resources and failure boundaries, with no drivers or subprocesses."""

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from taiyidsl import DeviceSetup, Experiment, LabConfig, Qubit, acquire, trig, us
from taiyidsl.runtime import session as session_module
from taiyidsl.runtime import worker as worker_module
from taiyidsl.runtime.session import HardwareSession
from taiyidsl.runtime.worker import Worker


def action(calls, name, error=None):
    def invoke(*args, **kwargs):
        calls.append(name)
        if error is not None:
            raise error

    return invoke


def session():
    lab = SimpleNamespace(
        emulation=False,
        setup=SimpleNamespace(_device_db={}, _spectrum_addresses={}, spectrum=None),
        signal_map={},
        qubits=(),
        artiq_python=sys.executable,
    )
    return HardwareSession(lab)


@pytest.mark.parametrize("has_acquisition", [False, True])
def test_session_run_checks_real_compiled_acquisitions_before_export(
    tmp_path, has_acquisition
):
    """Exercise the public artifact contract without opening a hardware worker."""
    device_db = {
        "core": {"class": "Core", "arguments": {"ref_period": 1e-9}},
        "camera": {"class": "TTLOut", "arguments": {"channel": 1}},
    }
    descriptor = {
        "backend": "artiq",
        "instruments": {"artiq": {"kind": "artiq", "device": "core"}},
        "channels": {"camera": {"instrument": "artiq", "device": "camera"}},
        "signals": {"q0.readout": "camera"},
    }
    lab = LabConfig(
        DeviceSetup.from_descriptor(descriptor, device_db=device_db), emulation=False
    )
    q = Qubit("q0")
    exp = Experiment("session_contract", [q])
    with exp.average(1), exp.realtime():
        if has_acquisition:
            acquire(q.readout, duration=10 * us, handle="image", qubits=[q])
        else:
            trig(q.readout, width=10 * us)
    compiled = lab.compile(exp)
    current = HardwareSession(lab)
    current.directory = tmp_path
    outgoing = io.StringIO()
    current._process = SimpleNamespace(poll=lambda: None, stdin=outgoing)
    receipt = {"source": "fake transport", "completed_shots": [{"point": 0, "shot": 0}]}
    current._messages.put(json.dumps({"ok": True, "result": receipt}))

    if has_acquisition:
        with pytest.raises(ValueError, match="no detector adapter"):
            current.run(compiled)
        assert outgoing.getvalue() == ""
        assert list(tmp_path.iterdir()) == []
        assert current._messages.qsize() == 1
    else:
        assert current.run(compiled) == receipt
        request = json.loads(outgoing.getvalue())
        assert request["command"] == "run"
        exported = Path(request["directory"])
        assert exported.parent == tmp_path
        assert (exported / "experiment.py").is_file()
        assert json.loads((exported / "execution_receipt.json").read_text()) == receipt


def test_worker_close_attempts_all_owned_resources_and_is_idempotent(tmp_path):
    calls = []
    primary = RuntimeError("invalidate failed")
    worker = Worker({}, tmp_path)
    worker.runner = SimpleNamespace(invalidate=action(calls, "invalidate", primary))
    worker.adapters = {
        "A": SimpleNamespace(close=action(calls, "A", OSError("close A failed"))),
        "B": SimpleNamespace(close=action(calls, "B")),
    }
    worker.manager = SimpleNamespace(close_devices=action(calls, "manager"))
    with pytest.raises(RuntimeError) as caught:
        worker.close()
    assert caught.value is primary
    assert any("close A failed" in note for note in primary.__notes__)
    assert calls == ["invalidate", "A", "B", "manager"]
    assert worker.runner is worker.manager is None
    assert worker.adapters == {}
    worker.close()
    assert calls == ["invalidate", "A", "B", "manager"]


def test_connect_failure_closes_partial_resources_without_masking_interrupt(
    tmp_path, monkeypatch
):
    from taiyidsl.runtime.spectrum import awg

    calls = []
    primary = KeyboardInterrupt("probe interrupted")

    class Manager:
        def __init__(self, database):
            calls.append("manager")

        def get(self, name):
            return SimpleNamespace(
                comm=SimpleNamespace(check_system_info=action(calls, "probe core"))
            )

        def close_devices(self):
            calls.append("close manager")

    class Adapter:
        def __init__(self, serial_number, **kwargs):
            self.name = serial_number
            self.clock_session_id = "test"

        def probe(self):
            calls.append("probe " + self.name)
            if self.name == "B":
                raise primary
            return {
                "model": "test",
                "serial_number": self.name,
                "sample_rate_hz": 1,
                "reference_clock_hz": 1,
                "is_demo": False,
                "clock_mode": "external_reference",
            }

        def close(self):
            calls.append("close " + self.name)
            if self.name == "A":
                raise OSError("A did not acknowledge close")

    monkeypatch.setitem(
        sys.modules, "artiq.master.databases", SimpleNamespace(DeviceDB=lambda p: p)
    )
    monkeypatch.setitem(
        sys.modules, "artiq.master.worker_db", SimpleNamespace(DeviceManager=Manager)
    )
    monkeypatch.setattr(awg, "SpcmAdapter", Adapter)
    config = {
        "device_db": {"core": {"arguments": {"host": "fixture.example"}}},
        "spectrum": {
            "groups": {
                "group": {
                    "cards": {
                        name: {
                            "model": "test",
                            "serial_number": name,
                            "sample_rate_hz": 1,
                            "reference_clock_hz": 1,
                        }
                        for name in ("A", "B")
                    }
                }
            }
        },
        "addresses": {},
        "clock_session_ids": {},
    }
    worker = Worker(config, tmp_path)
    with pytest.raises(KeyboardInterrupt) as caught:
        worker.connect()
    assert caught.value is primary
    assert any("A did not acknowledge close" in note for note in primary.__notes__)
    assert calls == [
        "manager",
        "probe core",
        "probe A",
        "probe B",
        "close A",
        "close B",
        "close manager",
    ]
    worker.close()
    assert calls[-1] == "close manager"


@pytest.mark.parametrize("failure_stage", [None, "load", "build", "run"])
def test_generated_program_scope_owns_only_its_module_and_added_paths(
    tmp_path, monkeypatch, failure_stage
):
    calls = []
    primary = KeyboardInterrupt("generated program interrupted")
    controls = SimpleNamespace(stage=failure_stage, error=primary)
    monkeypatch.setitem(sys.modules, "_taiyi_lifecycle_controls", controls)
    monkeypatch.setitem(
        sys.modules,
        "artiq.language.environment",
        SimpleNamespace(
            ProcessArgumentManager=lambda arguments: SimpleNamespace(
                check_unprocessed_arguments=lambda: None
            )
        ),
    )
    native_root = tmp_path / "native"
    existing_root = tmp_path / "existing"
    native_root.mkdir()
    existing_root.mkdir()
    native_name = "_taiyi_lifecycle_native"
    native_file = native_root / f"{native_name}.py"
    native_file.write_text("value = 42\n", encoding="utf-8")
    existing_file = existing_root / "existing_fixture.py"
    existing_file.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(existing_root))
    old_path = sys.path.copy()
    old_modules = {
        name for name in sys.modules if name.startswith("_taiyidsl_session_")
    }
    plan = {
        "points": [{"logical_events": []}],
        "metadata": {
            "native_artiq": {
                "package": {
                    "sources": [
                        {
                            "module": name,
                            "path": str(path),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                        for name, path in (
                            (native_name, native_file),
                            ("existing_fixture", existing_file),
                        )
                    ]
                }
            }
        },
    }
    (tmp_path / "compiled.json").write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "manifest.json").write_text('{"points": []}', encoding="utf-8")
    (tmp_path / "experiment.py").write_text(
        "from _taiyi_lifecycle_controls import stage, error\n"
        f"import {native_name}\n"
        "if stage == 'load':\n    raise error\n"
        "class GeneratedExperiment:\n"
        "    def __init__(self, managers):\n"
        "        if stage == 'build':\n            raise error\n"
        "    def run(self):\n"
        "        if stage == 'run':\n            raise error\n"
        "        self.result_adapter.after_shot(0, 0)\n",
        encoding="utf-8",
    )
    worker = Worker({}, tmp_path)
    worker.manager = SimpleNamespace()
    worker.runner = SimpleNamespace(
        abort=action(calls, "abort", OSError("abort failed"))
    )
    try:
        if failure_stage is None:
            for _ in range(2):
                receipt = worker.run(tmp_path)
                assert receipt["completed_shots"] == [{"point": 0, "shot": 0}]
            assert calls == []
        else:
            with pytest.raises(KeyboardInterrupt) as caught:
                worker.run(tmp_path)
            assert caught.value is primary
            assert any("abort failed" in note for note in primary.__notes__)
            assert calls == ["abort"]
        assert sys.path == old_path
        assert {
            name for name in sys.modules if name.startswith("_taiyidsl_session_")
        } == old_modules
        assert sys.modules[native_name].value == 42
    finally:
        sys.modules.pop(native_name, None)


def test_session_close_reaps_process_and_closes_all_streams_after_interrupt():
    calls = []
    primary = KeyboardInterrupt("wait interrupted")

    class Process:
        stdin = SimpleNamespace(
            write=action(calls, "send close"),
            flush=action(calls, "flush"),
            close=action(calls, "stdin", OSError("stdin failed")),
        )
        stdout = SimpleNamespace(close=action(calls, "stdout"))
        killed = False

        def poll(self):
            return None

        def wait(self, timeout):
            calls.append("wait")
            if not self.killed:
                raise primary

        def kill(self):
            calls.append("kill")
            self.killed = True

    current = session()
    current._process = Process()
    current._reader = SimpleNamespace(join=action(calls, "reader"))
    current._stderr = SimpleNamespace(close=action(calls, "log"))
    with pytest.raises(KeyboardInterrupt) as caught:
        current.close()
    assert caught.value is primary
    assert any("stdin failed" in note for note in primary.__notes__)
    assert calls == [
        "send close",
        "flush",
        "wait",
        "kill",
        "wait",
        "stdin",
        "stdout",
        "reader",
        "log",
    ]
    current.close()
    assert calls[-1] == "log"


def test_session_context_preserves_body_error_when_close_fails(monkeypatch):
    current = session()
    primary = ValueError("experiment failed")
    calls = []
    current._stderr = SimpleNamespace(close=action(calls, "log", OSError("log failed")))
    monkeypatch.setattr(current, "open", lambda: current)
    with pytest.raises(ValueError) as caught:
        with current:
            raise primary
    assert caught.value is primary
    assert any("log failed" in note for note in primary.__notes__)
    assert calls == ["log"]
    assert current._stderr is None


def test_worker_timeout_retains_unknown_outcome_when_cleanup_fails():
    current = session()
    calls = []
    current._stderr = SimpleNamespace(close=action(calls, "log", OSError("log failed")))
    with pytest.raises(TimeoutError, match="state is unknown") as caught:
        current._receive(0)
    assert any("log failed" in note for note in caught.value.__notes__)
    assert calls == ["log"]


def test_session_launch_uses_package_root_and_fresh_message_queue(
    tmp_path, monkeypatch
):
    current = session()
    current._messages.put("")
    observed = {}

    class Process:
        stdin = io.StringIO()
        stdout = io.StringIO('{"ok": true, "result": {"connected": true}}\n')
        status = None

        def poll(self):
            return self.status

        def wait(self, timeout):
            self.status = 0

    process = Process()

    def launch(command, **kwargs):
        observed.update(command=command, **kwargs)
        return process

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout):
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_module.subprocess, "Popen", launch)
    monkeypatch.setattr(session_module.threading, "Thread", Thread)
    assert current.open().observed == {"connected": True}
    assert observed["command"][3] == "taiyidsl.runtime.worker"
    package_root = str(Path(session_module.__file__).resolve().parents[2])
    assert (
        observed["env"]["PYTHONPATH"].split(session_module.os.pathsep)[0]
        == package_root
    )
    current.close()
    assert process.stdin.closed and process.stdout.closed and observed["stderr"].closed


def test_failed_process_launch_closes_worker_log(tmp_path, monkeypatch):
    current = session()
    primary = OSError("worker launch failed")
    observed = {}

    def launch(*args, **kwargs):
        observed.update(kwargs)
        raise primary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_module.subprocess, "Popen", launch)
    with pytest.raises(OSError) as caught:
        current.open()
    assert caught.value is primary
    assert observed["stderr"].closed
    assert current._stderr is current._process is None


def test_legacy_worker_module_remains_an_executable_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(worker_module, "main", action(calls, "main"))
    legacy = Path(worker_module.__file__).resolve().parents[1] / "session_worker.py"
    spec = importlib.util.spec_from_file_location("taiyidsl.session_worker", legacy)
    module = importlib.util.module_from_spec(spec)
    module.__name__ = "__main__"
    exec(spec.loader.get_code(spec.name), module.__dict__)
    assert calls == ["main"]
