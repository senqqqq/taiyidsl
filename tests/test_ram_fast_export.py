"""Fast RAM exports preserve phase tracking and avoid redundant SPI traffic."""

import ast
import importlib.util
from pathlib import Path

import pytest

from taiyidsl.artiq import generate_source, verify_equivalence, verify_export

SCRIPT = Path(__file__).parents[1] / "examples" / "ram_fast_replay.py"
spec = importlib.util.spec_from_file_location("fast_ram_example", SCRIPT)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def timed_body(source):
    tree = ast.parse(source)
    method = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "point_0"
    )
    loop = next(n for n in method.body if isinstance(n, ast.For))
    return ast.unparse(loop)


@pytest.mark.parametrize("alternating", [True, False])
def test_fast_long_sequence_is_bounded_and_matches_unrolled(alternating):
    compiled = example.build_sequence(alternating=alternating)
    assert verify_export(compiled)["offline_export_check"] == "passed"
    assert (
        verify_equivalence(example.build_sequence(count=8, alternating=alternating))[
            "status"
        ]
        == "passed"
    )
    source = generate_source(compiled)
    body = timed_body(source)
    assert source.count(".write_ram(") == 1
    for forbidden in ("set_cfr1", "set_ftw", "set_profile", "write_ram"):
        assert forbidden not in body
    assert ("set_pow" in body) == alternating
    assert ".io_update.pulse_mu(8)" in body


@pytest.mark.parametrize("mode", ["auto", "unrolled"])
def test_missing_phase_write_is_detected(mode):
    compiled = example.build_sequence(count=4)
    source = generate_source(compiled, mode=mode)
    if mode == "auto":
        old = "self.device_0.set_pow(int32(events[event_index, 5]))"
    else:
        event = next(
            e for e in compiled.points[0].physical_events if e.kind == "dds_ram_phase"
        )
        old = f"self.device_0.set_pow({event.params['ram_pow']})"
    assert old in source
    with pytest.raises(ValueError):
        verify_export(compiled, source=source.replace(old, "pass"))


def test_phase_budget_spi_boundary_and_real_bus_validation():
    minimum = example.build_sequence(count=2, phase_budget=416, gap_mu=528)
    assert verify_export(minimum)["offline_export_check"] == "passed"
    short = example.build_sequence(count=2, phase_budget=408, gap_mu=520)
    with pytest.raises(ValueError, match="driver SPI cursor minimum"):
        verify_export(short)
    source = generate_source(minimum).replace(
        "self.result_adapter = None",
        "self.result_adapter = None\n        self.device_0.bus.ref_period_mu = 16",
    )
    with pytest.raises(ValueError, match="SPI clock differs"):
        verify_export(minimum, source=source)


def test_manifest_reports_actual_replay_actions(tmp_path):
    import json

    for alternating, action, gap in ((True, "phase", 592), (False, "retrigger", 112)):
        compiled = example.build_sequence(count=4, alternating=alternating)
        artifact = compiled.export_artiq(tmp_path / action)
        manifest = json.loads(artifact["manifest"].read_text())
        (bank,) = manifest["ram_banks"]
        assert bank["replay_actions"][action] == 3
        assert bank["replay_actions"]["first"] == 1
        assert bank["resident_words"] == 401
        assert bank["min_gap_by_action_mu"] == {action: gap}
        assert bank["min_gap_mu"] == gap
        assert manifest["code_generation"]["ram_execution"] == "event_table"


def test_fast_paths_and_full_rearm_mix_in_one_shot_across_sweep_points():
    import math
    from dataclasses import replace

    from taiyidsl import (
        Experiment,
        FlatTopGaussian,
        Qubit,
        SweepParameter,
        delay,
        ns,
        play,
        reference_lab,
        us,
    )

    q = Qubit("q0")
    offset = SweepParameter("phase_offset", [0, 0.2], unit="rad")
    exp = Experiment("mixed_ram_paths", [q])
    pulse = FlatTopGaussian(4 * us, sigma=0.5 * us, frequency=125e6)
    with exp.average(2):
        with exp.sweep(offset):
            with exp.realtime():
                # first, retrigger, phase, rearm, retrigger, phase, rearm
                for amplitude, phase, frequency in (
                    (0.5, offset, 125e6),
                    (0.5, offset, 125e6),
                    (0.5, math.pi / 2, 125e6),
                    (0.25, math.pi / 2, 125e6),
                    (0.25, math.pi / 2, 125e6),
                    (0.25, 0, 125e6),
                    (0.25, 0, 126e6),
                ):
                    play(
                        q.raman,
                        pulse,
                        amplitude=amplitude,
                        phase=phase,
                        frequency=frequency,
                    )
                    delay(q.raman, 4112 * ns)
    lab = reference_lab([q])
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(
        lab.setup.devices[name],
        nco_mode="legacy_tracking",
        ram_latency_mu=112,
        ram_rearm_duration_mu=4000,
        ram_phase_update_duration_mu=480,
    )
    compiled = lab.compile(exp)
    for point in compiled.points:
        assert [
            e.params["ram_replay_action"]
            for e in point.physical_events
            if e.kind == "dds_ram_start"
        ] == ["first", "retrigger", "phase", "rearm", "retrigger", "phase", "rearm"]
    assert verify_equivalence(compiled)["status"] == "passed"
