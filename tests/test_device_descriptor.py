"""Database-backed authoring contracts; no driver import or instrument IO."""

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from taiyidsl import DeviceSetup, Experiment, LabConfig, Qubit, Square, ns, play
from taiyidsl.config.descriptor import _artiq_device, read_database
from taiyidsl.runtime.session import HardwareSession
from taiyidsl.runtime.worker import Worker


@pytest.mark.parametrize("mode", [None, "dds"])
def test_standalone_descriptor_roundtrip_and_mode_selection(mode):
    descriptor = {
        "backend": "spectrum",
        "instruments": {"bench": {"kind": "spectrum", "device": "card"}},
        "channels": {"out": {"instrument": "bench", "channel": 0}},
        "signals": {"q0": {"rf": "out"}},
    }
    spec_db = {"card": {"serial_number": "OFFLINE", "active_channels": [0]}}
    if mode:
        spec_db["card"]["mode"] = mode
    setup = DeviceSetup.from_descriptor(descriptor, spec_db=spec_db)
    selected = setup.describe()["instruments"]["bench"]["settings"]
    assert selected["mode"] == (mode or "awg")
    assert selected["clock_source"] == "internal"
    assert selected["trigger_source"] == "software"
    assert set(setup.describe()["databases"]) == {"spec_db"}
    assert setup.trigger_connections() == []
    lab = LabConfig(setup)
    assert lab.backend == "spectrum"
    with pytest.raises(RuntimeError, match="emulation"):
        lab.connect()
    restored = LabConfig.from_dict(json.loads(json.dumps(lab.to_dict())))
    assert restored.to_dict() == lab.to_dict()
    q = Qubit("q0")
    pulse = Square(640 * ns, amplitude=0.2, frequency=10e6)
    exp = Experiment("card", [q])
    with exp.average(1), exp.realtime():
        play(q.rf, pulse)
    assert (
        lab.compile(exp).points[0].physical_events
        == restored.compile(exp).points[0].physical_events
    )
    descriptor["instruments"]["second"] = {"kind": "spectrum", "device": "card"}
    with pytest.raises(ValueError, match="exactly one"):
        DeviceSetup.from_descriptor(descriptor, spec_db=spec_db)


def databases():
    device_db = {
        "core": {
            "type": "local",
            "module": "artiq.coredevice.core",
            "class": "Core",
            "arguments": {"host": None, "ref_period": 1e-9},
        },
        "trig_a": {
            "type": "local",
            "module": "artiq.coredevice.ttl",
            "class": "TTLOut",
            "arguments": {"channel": 1},
        },
        "trig_b": {
            "type": "local",
            "module": "artiq.coredevice.ttl",
            "class": "TTLOut",
            "arguments": {"channel": 2},
        },
        "camera": {
            "type": "local",
            "module": "artiq.coredevice.ttl",
            "class": "TTLOut",
            "arguments": {"channel": 3},
        },
        "trigger_a": "trig_a",
    }
    spec_db = {
        name: {"serial_number": f"SIM_{name}", "active_channels": [0, 1]}
        for name in ("A", "B")
    }
    descriptor = {
        "name": "descriptor_example",
        "backend": "artiq-spectrum",
        "instruments": {
            "artiq": {"kind": "artiq", "device": "core"},
            "A": {"kind": "spectrum", "device": "A"},
            "B": {"kind": "spectrum", "device": "B"},
        },
        "channels": {
            "nuclear_rf": {"instrument": "A", "channel": 0},
            "clock": {"instrument": "B", "channel": 0},
            "camera": {"instrument": "artiq", "device": "camera"},
        },
        "sync_groups": {
            "qubit_awg": {
                "cards": ["A", "B"],
                "triggers": {"A": "trigger_a", "B": "trig_b"},
            }
        },
        "signals": {"q0.rf": "nuclear_rf", "q0.clock": "clock", "q0.readout": "camera"},
    }
    return descriptor, device_db, spec_db


def test_static_database_rejects_execution_and_preserves_constants(tmp_path):
    source = tmp_path / "device_db.py"
    source.write_text('TICK = 1 / 1000000000\ndevice_db = {"tick": TICK}\n')
    data, pin = read_database(source, "device_db")
    assert data == {"tick": 1e-9} and pin["sha256"]
    source.write_text('open("SHOULD_NOT_EXIST", "w")\ndevice_db = {}\n')
    with pytest.raises(ValueError, match="No database code was executed"):
        read_database(source, "device_db")
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert (
        read_database({"host": "one"}, "device_db")[1]
        != read_database({"host": "two"}, "device_db")[1]
    )


def test_dds_clock_uses_artiq9_cpld_divider_and_pll_contract():
    database = {
        "spi": {"class": "SPIMaster"},
        "cpld": {
            "class": "CPLD",
            "arguments": {
                "refclk": 125e6,
                "clk_div": 0,
                "spi_device": "spi",
            },
        },
        "dds": {
            "class": "AD9910",
            "arguments": {
                "cpld_device": "cpld",
                "chip_select": 4,
            },
        },
    }
    assert _artiq_device(database, "dds").sysclk_hz == 1e9
    database["dds"]["arguments"]["pll_en"] = 0
    assert _artiq_device(database, "dds").sysclk_hz == 31_250_000
    database["dds"]["arguments"]["pll_n"] = 16
    with pytest.raises(ValueError, match="does not accept a pll_n"):
        _artiq_device(database, "dds")


def test_descriptor_compilation_and_legacy_profile_roundtrip():
    descriptor, device_db, spec_db = databases()
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    lab = LabConfig(setup)
    assert lab.backend == "artiq_spectrum"
    assert setup.trigger_connections()[0]["device"] == "trig_a"
    assert setup.connections()[0]["port"] == 0
    q = Qubit("q0")
    exp = Experiment("simple", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        play(q.rf, Square(100 * ns, amplitude=0.2, frequency=30000))
    compiled = lab.compile(exp)
    compiled.assert_integrity()
    assert "device_setup" not in exp.metadata
    assert compiled.metadata["device_setup"]["connections"] == setup.connections()
    legacy = LabConfig.from_dict(lab.to_dict())
    assert legacy.to_dict() == lab.to_dict()
    assert (
        legacy.compile(exp).points[0].physical_events
        == compiled.points[0].physical_events
    )


def test_grouped_signals_match_flat_routes_and_describe_database_connections():
    descriptor, device_db, spec_db = databases()
    device_db["core"]["arguments"]["host"] = "core.example"
    spec_db["A"]["address"] = "/dev/spcm0"
    descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
    descriptor["signals"] = {
        "q0.rf": "nuclear_rf",
        "q0.clock": "clock",
        "q1.rf": "nuclear_rf",
        "camera": "camera",
    }
    flat = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
    descriptor["signals"] = {
        "q0": {"rf": "nuclear_rf", "clock": "clock"},
        "q1": {"rf": "nuclear_rf"},
        "camera": "camera",
    }
    grouped = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    assert grouped.to_dict() == flat.to_dict()
    assert grouped.connections() == flat.connections()
    assert LabConfig(grouped).to_dict() == LabConfig(flat).to_dict()
    q0 = Qubit(
        "q0",
        signals={"rf": "q0.rf", "clock": "q0.clock"},
        parameters={"rf": {}, "clock": {}},
    )
    lab = LabConfig(grouped, qubits=[q0])
    view = lab.describe()
    instruments = view["setup"]["instruments"]
    assert instruments["artiq"]["connection"]["host"] == "core.example"
    assert instruments["A"]["connection"] == {
        "driver": "spcm.Card",
        "address": "/dev/spcm0",
    }
    assert instruments["B"]["connection"] == {
        "driver": "spcm.Card",
        "serial_number": "SIM_B",
    }
    assert view["qubits"]["q0"] == q0.to_dict()
    assert view["emulation"] is True
    instruments["A"]["settings"]["output_range_mv"] = -1
    view["setup"]["signals"]["q0"].clear()
    assert (
        lab.describe()["setup"]["instruments"]["A"]["settings"]["output_range_mv"]
        == 500
    )
    assert lab.describe()["setup"]["signals"]["q0"]["rf"] == "nuclear_rf"


def test_grouped_signals_reject_duplicate_and_ambiguous_names():
    descriptor, device_db, spec_db = databases()
    invalid = [
        ({"q0": {"rf": "nuclear_rf"}, "q0.rf": "nuclear_rf"}, "Duplicate"),
        ({"q0.rf": {"clock": "clock"}}, "undotted qubit"),
        ({"q0": {"rf.extra": "nuclear_rf"}}, "roles.*undotted"),
        ({"q0": {"rf": {"extra": "nuclear_rf"}}}, "laboratory channel"),
    ]
    for signals, error in invalid:
        descriptor["signals"] = signals
        with pytest.raises(ValueError, match=error):
            DeviceSetup.from_descriptor(
                descriptor, device_db=device_db, spec_db=spec_db
            )


def test_lab_uses_experiment_calibration_and_frames_without_rebuilding(tmp_path):
    from taiyidsl import shift_phase

    descriptor, device_db, spec_db = databases()
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    q = Qubit(
        "q0",
        signals={"rf": "q0.rf"},
        parameters={"rf": {"frequency": 30000, "pi2_amplitude": 0.2}},
    )
    lab = LabConfig(setup, qubits=[q])
    pulse = Square(400 * ns)

    def experiment():
        exp = Experiment("calibration_at_construction", [q])
        with exp.average(1), exp.realtime():
            with exp.triggered_sequence(group="qubit_awg"):
                shift_phase(q.drive_frame("rf"), 0.4)
                play(q.rf, pulse, amplitude=q.parameters.rf.pi2_amplitude)
        return exp

    original = experiment()
    old = lab.compile(original)
    old_data = old.to_dict()
    lab_data = lab.describe()
    q.parameters.rf.pi2_amplitude = 0.3
    q.parameters.rf.frame = "rf_control"
    edited = experiment()
    current = lab.compile(edited)
    current_event = next(
        event for event in current.points[0].logical_events if event.kind == "play"
    )
    assert current_event.params["amplitude"] == 0.3
    assert current_event.params["frame_phase"] == pytest.approx(0.4)
    assert current.qubits["q0"].to_dict() == edited.qubits["q0"].to_dict()
    assert current.setup.spectrum.signal_frames["q0.rf"] == "q0__phase__rf_control"
    assert old.setup.spectrum.signal_frames["q0.rf"] == "q0"
    assert old.to_dict() == old_data
    assert lab.compile(original).to_dict() == old_data
    assert lab.describe() == lab_data
    old.assert_integrity()

    # Both compile routes use exactly the same experiment snapshot and topology.
    store = lab.compile_stream(edited, directory=tmp_path / "compiled")
    chunk = store.load_chunk(0)
    assert chunk.qubits["q0"].to_dict() == current.qubits["q0"].to_dict()
    assert chunk.setup.spectrum.signal_frames == current.setup.spectrum.signal_frames
    assert chunk.metadata["device_setup"] == current.metadata["device_setup"]
    # Disk JSON represents tuple-valued contracts/IDs as lists.
    assert json.loads(
        json.dumps(chunk.to_dict()["points"][0]["physical_events"])
    ) == json.loads(json.dumps(current.to_dict()["points"][0]["physical_events"]))

    # Reusing a registered ID for a different logical route still requires a
    # deliberate LabConfig change, even when that alternative route is mapped.
    rerouted = Qubit(
        "q0", signals={"rf": "q0.clock"}, parameters={"rf": {"frequency": 30000}}
    )
    mismatch = Experiment("changed_topology", [rerouted])
    with pytest.raises(ValueError, match="signal topology"):
        lab.compile(mismatch)
    with pytest.raises(ValueError, match="signal topology"):
        lab.compile_stream(mismatch, directory=tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_shared_output_is_explicit_and_mapping_is_detached():
    descriptor, device_db, spec_db = databases()
    descriptor["signals"]["q1.rf"] = "nuclear_rf"
    with pytest.raises(ValueError, match="sharing='global'"):
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
    descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    assert copy.deepcopy(setup).shared_outputs == {"nuclear_rf": ("q0.rf", "q1.rf")}
    descriptor["signals"].clear()
    setup.connections()[0]["device"] = "incorrect"
    assert setup.connections()[0]["device"] == "nuclear_rf"
    with pytest.raises(RuntimeError, match="emulation=True"):
        LabConfig(setup).connect()


def test_missing_device_and_duplicate_output_rejected():
    descriptor, device_db, spec_db = databases()
    descriptor["sync_groups"]["qubit_awg"]["triggers"]["A"] = "missing"
    with pytest.raises(ValueError, match="Unknown ARTIQ device"):
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
    descriptor, device_db, spec_db = databases()
    descriptor["channels"]["duplicate"] = {"instrument": "A", "channel": 0}
    with pytest.raises(ValueError, match="Duplicate physical channel"):
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
    descriptor, device_db, spec_db = databases()
    device_db["camera_copy"] = copy.deepcopy(device_db["camera"])
    descriptor["channels"]["camera_copy"] = {
        "instrument": "artiq",
        "device": "camera_copy",
    }
    with pytest.raises(ValueError, match="Duplicate physical channel"):
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)


def test_trigger_routes_reuse_declared_ttl_channels_without_new_logical_signals():
    descriptor, device_db, spec_db = databases()
    descriptor["channels"]["awg_line"] = {
        "instrument": "artiq",
        "device": "trigger_a",
        "timing": {"grid_mu": 8},
    }
    descriptor["channels"]["trig_b"] = {
        "instrument": "artiq",
        "device": "trig_b",
    }
    route = descriptor["sync_groups"]["qubit_awg"]["triggers"]
    route["A"] = "awg_line"
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    assert setup.spectrum.groups["qubit_awg"].card_trigger_devices == {
        "A": "trig_a",
        "B": "trig_b",
    }
    assert setup.devices["trig_a"].grid_mu == 8
    assert set(LabConfig(setup).signal_map) == set(descriptor["signals"])
    # Existing direct database routes retain exactly the same resolved contract.
    route["A"] = "trigger_a"
    legacy = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    assert legacy.to_dict() == setup.to_dict()
    route["A"] = "awg_line"
    device_db["awg_line"] = "trig_b"
    with pytest.raises(ValueError, match="Ambiguous trigger"):
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)


def test_worker_runs_existing_host_loop_and_keeps_receipts(tmp_path, monkeypatch):
    # Exercise the transport target without importing ARTIQ or a Spectrum SDK.
    class ArgumentManager:
        def __init__(self, arguments):
            assert arguments == {}

        def check_unprocessed_arguments(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "artiq.language.environment",
        SimpleNamespace(ProcessArgumentManager=ArgumentManager),
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"backend": "artiq_spectrum", "points": []})
    )
    (tmp_path / "compiled.json").write_text(
        json.dumps({"points": [{"logical_events": []}]})
    )
    (tmp_path / "experiment.py").write_text(
        "class GeneratedExperiment:\n"
        "    def __init__(self, managers):\n"
        "        assert managers[2] is not None\n"
        "        self.managers = managers\n"
        "    def run(self):\n"
        "        self.result_adapter.before_shot(0, 0)\n"
        "        self.result_adapter.after_shot(0, 0)\n"
        "        self.result_adapter.finish()\n"
    )
    worker = Worker({}, tmp_path)
    worker.manager = SimpleNamespace()
    result = worker.run(tmp_path)
    assert result["completed_shots"] == [{"point": 0, "shot": 0}]
    assert result["measurement_data"] is None
    assert result["physical_acceptance"] == "not_verified"


def test_connect_only_probes_hardware_and_accepts_equal_numeric_clocks(
    tmp_path, monkeypatch
):
    from taiyidsl.runtime.spectrum import awg as spectrum_runtime

    descriptor, device_db, spec_db = databases()
    device_db["actual_core"] = device_db["core"]
    device_db["actual_core"]["arguments"]["host"] = "core.example"
    device_db["core"] = "actual_core"
    spec_db["A"]["reference_clock_hz"] = 125e6
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    calls = []

    class Manager:
        def __init__(self, database):
            pass

        def get(self, name):
            assert name == "core"
            return SimpleNamespace(
                comm=SimpleNamespace(
                    check_system_info=lambda: calls.append("core_probe")
                )
            )

    class Adapter:
        def __init__(self, serial, **kwargs):
            self.serial = serial

        def probe(self):
            calls.append(self.serial)
            return dict(
                model="M4i.6631-x8",
                serial_number=self.serial,
                sample_rate_hz=1_250_000_000,
                reference_clock_hz=125_000_000,
                is_demo=False,
                clock_mode="external_reference",
            )

        def configure(self, contract):
            raise AssertionError("connect must not configure hardware")

    monkeypatch.setitem(
        sys.modules, "artiq.master.databases", SimpleNamespace(DeviceDB=lambda p: p)
    )
    monkeypatch.setitem(
        sys.modules, "artiq.master.worker_db", SimpleNamespace(DeviceManager=Manager)
    )
    monkeypatch.setattr(spectrum_runtime, "SpcmAdapter", Adapter)
    worker = Worker(
        {
            "device_db": device_db,
            "spectrum": setup.spectrum.to_dict(),
            "addresses": {},
        },
        tmp_path,
    )
    assert worker.connect()["hardware_executed"] is False
    assert calls == ["core_probe", "SIM_A", "SIM_B"]


def test_session_snapshot_handles_immutable_qubits_without_opening_hardware():
    descriptor, device_db, spec_db = databases()
    descriptor["signals"]["q0.raman"] = descriptor["signals"].pop("q0.clock")
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    lab = LabConfig(setup, qubits=[Qubit("q0")], emulation=False)
    session = HardwareSession(lab)
    assert session._process is None
    assert session.lab.qubits[0].to_dict() == lab.qubits[0].to_dict()
    assert session.lab.setup is not lab.setup
