"""N-card contracts, synchronous edges and atomic host preflight/launch."""

import ast
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_block_trigger import _guard_namespace
from test_spectrum_banks import ResidentAdapter
from test_spectrum_runtime import FakeAdapter, contract, program

from taiyidsl import (
    CompileError,
    ControlBinding,
    ControlRecipe,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    ns,
    play,
)
from taiyidsl.spectrum_banks import build_manifest_banks
from taiyidsl.spectrum_export import artiq_subplan
from taiyidsl.spectrum_runtime import SpectrumRunner, SpectrumRuntimeError


def n_card_lab(count, *, lanes=8):
    names = tuple(chr(ord("A") + i) for i in range(count))
    group = SpectrumGroup(
        cards={name: SpectrumCard(f"SIM_{name}") for name in names},
        outputs={f"rf_{name}": SpectrumOutput(name, 0) for name in names},
        trigger_devices={name: f"start_{name}" for name in names},
        trigger_signal="start.all",
    )
    devices = {
        **{f"rf_{name}": Device(f"rf_{name}", "spectrum") for name in names},
        **{
            f"start_{name}": Device(f"start_{name}", "ttl", grid_mu=1) for name in names
        },
    }
    signals = SignalMap({f"q{name}.rf": f"rf_{name}" for name in names})
    signals["start.all"] = ControlRecipe(
        tuple(ControlBinding(f"start_{n}") for n in names)
    )
    return LabConfig(
        DeviceSetup(
            devices, spectrum=SpectrumConfig({"g": group}), rtio_lane_count=lanes
        ),
        signals,
        "artiq_spectrum",
    )


def n_card_experiment(count):
    qubits = [Qubit(f"q{chr(ord('A') + i)}") for i in range(count)]
    exp = Experiment("n_card", qubits)
    with (
        exp.average(1),
        exp.realtime(),
        exp.triggered_sequence(group="g", trigger="start.all"),
    ):
        with exp.parallel():
            for q in qubits:
                play(q.rf, Square(400 * ns, amplitude=0.2, frequency=30_000))
    return exp


@pytest.mark.parametrize("count", [2, 3, 4])
def test_n_card_routes_roundtrip_equal_edges_and_native_trigger_count(count):
    profile = n_card_lab(count)
    assert LabConfig.from_dict(profile.to_dict()).to_dict() == profile.to_dict()
    compiled = profile.compile(n_card_experiment(count))
    edges = {}
    for event in compiled.points[0].physical_events:
        if event.kind in {"ttl_on", "ttl_off"}:
            edges.setdefault(event.signal, []).append((event.kind, event.start_mu))
    assert len(edges) == count
    assert all(value == next(iter(edges.values())) for value in edges.values())
    native = artiq_subplan(compiled)
    assert (
        sum(event.kind == "trigger" for event in native.points[0].logical_events)
        == count
    )
    group = next(
        e.params
        for e in compiled.points[0].physical_events
        if e.kind == "spectrum_program"
    )
    assert len(group["cards"]) == count
    assert all(
        c["contract"]["trigger_device"] == f"start_{name}"
        for name, c in group["cards"].items()
    )


def test_fourth_trigger_requires_declared_rtio_capacity():
    with pytest.raises(CompileError, match="SED lane capacity"):
        n_card_lab(4, lanes=3).compile(n_card_experiment(4))


def test_fourth_trigger_cannot_be_omitted_or_alias_first_hardware_channel():
    profile = n_card_lab(4)
    profile.signal_map["start.all"] = ControlRecipe(
        tuple(ControlBinding(f"start_{name}") for name in ("A", "B", "C"))
    )
    with pytest.raises(CompileError, match="configured.*physical path"):
        profile.compile(n_card_experiment(4))
    compiled = n_card_lab(4).compile(n_card_experiment(4))
    namespace, invocation = _guard_namespace(compiled)
    devices = {
        f"start_{name}": SimpleNamespace(channel=i) for i, name in enumerate("ABCD")
    }
    namespace["self"] = SimpleNamespace(get_device=devices.__getitem__)
    code = compile(
        ast.Module(body=[invocation], type_ignores=[]), "n_card_guard", "exec"
    )
    exec(code, namespace)
    devices["start_D"] = SimpleNamespace(channel=devices["start_A"].channel)
    with pytest.raises(RuntimeError, match="trigger hardware resource alias"):
        exec(code, namespace)


def test_fourth_card_memory_is_checked_without_moving_output_edges():
    profile = n_card_lab(4)
    group = profile.setup.spectrum.groups["g"]
    group = replace(
        group, cards={**group.cards, "D": replace(group.cards["D"], memory_bytes=128)}
    )
    profile.setup.spectrum = replace(profile.setup.spectrum, groups={"g": group})
    with pytest.raises(CompileError, match="memory|capacity"):
        profile.compile(n_card_experiment(4))


def runtime_fixture(count, schema):
    log = []
    specs = {f"card{i}": contract(str(101 + i)) for i in range(count)}
    kind = ResidentAdapter if schema == 2 else FakeAdapter
    cards = {name: kind(name, spec, log) for name, spec in specs.items()}
    groups = [
        {
            "name": "g",
            "calibration": {"status": "unmeasured"},
            "cards": {
                name: {"contract": spec, "program": program(spec)}
                for name, spec in specs.items()
            },
        }
    ]
    manifest = {"schema_version": schema, "points": [{"index": 0, "groups": groups}]}
    if schema == 2:
        manifest["compiled_sha256"] = "n-card-test"
        manifest["host_execution_plan"] = {"chunks": [{"index": 0, "points": [0]}]}
        manifest["banks"] = build_manifest_banks(
            manifest["points"], manifest["host_execution_plan"]
        )
    return log, cards, manifest


@pytest.mark.parametrize("count", [2, 3, 4])
@pytest.mark.parametrize("schema", [1, 2])
def test_all_cards_ready_before_one_artiq_launch(count, schema):
    log, cards, manifest = runtime_fixture(count, schema)
    result = SpectrumRunner(cards, allow_unverified=True).run_point(
        manifest, 0, lambda point: log.append((point, "artiq"))
    )
    launch = log.index((0, "artiq"))
    assert all(log.index((name, "ready")) < launch for name in cards)
    assert all(
        log.index((name, "upload")) < log.index((next(iter(cards)), "arm"))
        for name in cards
    )
    assert log.count((0, "artiq")) == 1
    assert result["status"] == "complete"


@pytest.mark.parametrize("count", [3, 4])
@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("stage", ["configure", "upload", "arm", "ready", "complete"])
def test_failure_on_last_card_stops_group_and_does_not_reuse_bank(count, schema, stage):
    log, cards, manifest = runtime_fixture(count, schema)
    last = cards[f"card{count - 1}"]
    last.failure = stage
    runner = SpectrumRunner(cards, allow_unverified=True)
    with pytest.raises(SpectrumRuntimeError, match=stage):
        runner.run_point(manifest, 0, lambda point: log.append((point, "artiq")))
    assert all((name, "stop") in log for name in cards)
    if stage != "complete":
        assert (0, "artiq") not in log
    last.failure = None
    log.clear()
    runner.run_point(manifest, 0, lambda _: None)
    assert all((name, "upload") in log for name in cards)


@pytest.mark.parametrize("schema", [1, 2])
def test_fourth_card_bad_preflight_performs_no_hardware_mutation(schema):
    log, cards, manifest = runtime_fixture(4, schema)
    cards["card3"].spec = {**cards["card3"].spec, "serial_number": "wrong"}
    with pytest.raises(SpectrumRuntimeError, match="serial_number"):
        SpectrumRunner(cards, allow_unverified=True).run_point(
            manifest, 0, lambda _: None
        )
    assert all(operation == "probe" for _, operation in log)


@pytest.mark.parametrize("schema", [1, 2])
def test_fourth_card_never_ready_prevents_launch_and_stops_group(schema):
    log, cards, manifest = runtime_fixture(4, schema)
    cards["card3"].becomes_ready = False
    with pytest.raises(SpectrumRuntimeError, match="did not become ready"):
        SpectrumRunner(cards, allow_unverified=True, ready_timeout_s=0.002).run_point(
            manifest, 0, lambda point: log.append((point, "artiq"))
        )
    assert (0, "artiq") not in log
    assert all((name, "stop") in log for name in cards)


def test_default_dual_ttl_and_explicit_legacy_config_have_identical_analog_mapping():
    directory = Path(__file__).resolve().parents[1] / "examples" / "artiq_spectrum"
    new = json.loads((directory / "lab_simulated.json").read_text(encoding="utf-8"))
    old = json.loads(
        (directory / "lab_legacy_shared_trigger.json").read_text(encoding="utf-8")
    )
    new_group = new["setup"]["spectrum"]["groups"]["qubit_awg"]
    old_group = old["setup"]["spectrum"]["groups"]["qubit_awg"]
    assert new_group["trigger_devices"] == {"A": "awg_trigger_a", "B": "awg_trigger_b"}
    assert old_group["trigger_device"] == "awg_trigger"
    assert new_group["outputs"] == old_group["outputs"]
    assert new["signals"] == old["signals"]
    assert LabConfig.from_dict(copy.deepcopy(new))
    assert LabConfig.from_dict(copy.deepcopy(old))
