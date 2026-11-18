"""GreenMOT keeps its real final IO boundary before the Ramsey delay."""

import ast
import math
import runpy
from pathlib import Path

import pytest

from taiyidsl import CompileError, LabConfig, ns

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/artiq_spectrum/greenmot_ramsey.py"

# Independently transcribed preparation body observed in the reviewed upstream
# _GreenMOTbase.py. No hardware or imports are executed by the AST comparison.
UPSTREAM_PREPARATION = """
class _GreenMOTbase:
    def prepare_GreenMOT(self):
        self.start_lasers()
        self.start_B_field()
        self.AOM_399_Push.sw.off()
        self.ttl_399_Push_Sh.off()
        self.AOM_399_2DMOT.sw.off()
        self.AOM_399_zeeman.sw.off()
        self.core.break_realtime()
        delay(10 * ms)
        self.start_556_fixed_sweep()
        delay(5 * ms)
        self.ttl_399_3DMOT_Sh.off()
        self.AOM_399_3DMOT.sw.off()
        for i in range(100):
            self.zotino0.set_dac([3.5 + (0.15 - 3.5) / 100 * i], [3])
            delay((30 / 100) * ms)
        delay(5 * ms)
        self.core.break_realtime()
        self.compress_green_MOT_DDS()
        self.ttl_399_Probe_Sh.on()
        delay(15 * ms)
        self.AOM_556_3DMOT.sw.off()
        self.zotino0.set_dac([-0.95, 0.0], [2, 3])
"""


@pytest.fixture(scope="module")
def example():
    return runpy.run_path(str(EXAMPLE))


def test_split_reconstructs_reviewed_preparation_without_running_source(
    example, tmp_path
):
    source = tmp_path / "_GreenMOTbase.py"
    source.write_text(
        "raise RuntimeError('must never import this source')\n" + UPSTREAM_PREPARATION,
        encoding="utf-8",
    )
    example["validate_greenmot_split"](source)
    # Documentation and formatting may change without changing the operations.
    source.write_text(
        ast.unparse(ast.parse(UPSTREAM_PREPARATION)) + "\n# extra comment\n",
        encoding="utf-8",
    )
    example["validate_greenmot_split"](source)


@pytest.mark.parametrize(
    "old,new",
    [
        ("delay(15 * ms)", "delay(14 * ms)"),
        ("[-0.95, 0.0]", "[-0.90, 0.0]"),
        ("self.start_lasers()", "self.start_B_field()"),
        ("self.core.break_realtime()", "self.core.reset()"),
    ],
)
def test_changed_upstream_preparation_requires_split_review(
    example, tmp_path, old, new
):
    source = tmp_path / "_GreenMOTbase.py"
    source.write_text(UPSTREAM_PREPARATION.replace(old, new), encoding="utf-8")
    with pytest.raises(ValueError, match="preparation changed"):
        example["validate_greenmot_split"](source)


def test_split_requires_explicit_final_ldac_completion(example, tmp_path):
    source = tmp_path / "_GreenMOTbase.py"
    source.write_text(UPSTREAM_PREPARATION, encoding="utf-8")
    adapter = tmp_path / "wrong_adapter.py"
    original = (example["NATIVE"] / "greenmot_stages.py").read_text(encoding="utf-8")
    adapter.write_text(
        original.replace("delay_mu(2 * self.zotino0.bus.ref_period_mu)", "delay_mu(0)"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="preparation changed"):
        example["validate_greenmot_split"](source, adapter)


def test_actual_preparation_end_to_gate_and_carrier_phase(example):
    compiled = LabConfig.load(EXAMPLE.with_name("lab_simulated.json")).compile(
        example["build_experiment"]()
    )
    native = compiled.metadata["native_artiq"]
    assert native["execution"] == "single_top_level_kernel"
    assert compiled.metadata["analog_settling_verified"] is False
    expected_p2 = (18, 270, 270)
    for index, point in enumerate(compiled.points):
        entry = native["points"][index]
        assert [c["method"] for c in entry["prefix"]] == ["prepare_MOT"]
        assert [c["method"] for c in entry["timed"]] == ["finish_preparation"]
        assert entry["timed"][0]["duration_mu"] == 15_000_016
        assert "TTL falling edge" in entry["timed"][0]["end_boundary"]
        region = next(e for e in point.logical_events if e.kind == "spectrum_region")
        start_s = float(region.start_mu * compiled.setup.tick_seconds)
        assert start_s == pytest.approx(0.015010016, abs=1e-15)
        assert start_s - 0.015000016 == pytest.approx(10e-6, abs=1e-15)
        program = next(e for e in point.physical_events if e.kind == "spectrum_program")
        pulses = program.params["cards"]["A"]["program"]["pulses"]
        assert pulses[0]["phase_rad"] == 0
        assert math.degrees(pulses[1]["phase_rad"]) % 360 == pytest.approx(
            expected_p2[index]
        )
        assert compiled.virtual_z_frames(index)["q"] == pytest.approx(math.pi / 2)
        triggers = set(compiled.setup.spectrum.groups["qubit_awg"].trigger_device_names)
        edges = [e for e in point.physical_events if e.signal in triggers]
        for kind in ("ttl_on", "ttl_off"):
            selected = [e for e in edges if e.kind == kind]
            assert {e.signal for e in selected} == triggers
            assert len(selected) == len(triggers)
            assert len({e.start_mu for e in selected}) == 1


def test_gate_delay_does_not_hide_trigger_in_preparation(example, tmp_path):
    lab = LabConfig.load(EXAMPLE.with_name("lab_simulated.json"))
    with pytest.raises(CompileError, match="(?i)native|prelude|trigger|timed"):
        lab.compile(
            example["build_experiment"](delays_ms=(0.01,), preparation_to_gate=400 * ns)
        ).export(tmp_path)
