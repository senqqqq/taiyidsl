"""Executable mixed DDS exports preserve separate state and common RTIO timing."""

import json
import math
from dataclasses import replace

import pytest
from test_mixed_polar_backend import envelope, mixed_lab, selected, square

from taiyidsl import Experiment, SweepParameter, delay, ns, play, rz
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_equivalence, verify_export


def build_mixed(*, duration=800, revision=8, count=1, xy=False, offset=0):
    qubits, lab = mixed_lab(revision=revision)
    experiment = Experiment("mixed_export", qubits)
    with experiment.average(2), experiment.realtime(), experiment.parallel():
        with experiment.serial():
            for index in range(count):
                play(
                    qubits[0].raman,
                    envelope(duration),
                    phase=(index % 2) * math.pi / 2 if xy else 0,
                )
        with experiment.serial():
            if offset:
                delay(qubits[1].raman, offset * ns)
            with experiment.repeat(count):
                play(qubits[1].raman, square(duration))
        with experiment.serial(), experiment.repeat(count):
            play(qubits[2].raman, envelope(duration))
    return lab.compile(experiment)


@pytest.mark.parametrize("duration", [200, 800])
@pytest.mark.parametrize("revision", [8, 9])
def test_three_dds_modes_single_gate_share_exact_boundaries(duration, revision):
    compiled = build_mixed(duration=duration, revision=revision)
    result = verify_equivalence(compiled)
    assert result["status"] == "passed"
    assert compiled.points[0].duration_mu == duration
    assert len(selected(compiled, "dds_nco_epoch")) == 3


@pytest.mark.parametrize("revision", [8, 9])
def test_200ns_same_profile_repetition_uses_no_per_gate_spi(revision):
    compiled = build_mixed(duration=200, count=8, revision=revision)
    assert verify_equivalence(compiled)["status"] == "passed"
    assert len(selected(compiled, "dds_polar_select")) == 1
    assert len(selected(compiled, "dds_polar_retrigger")) == 7
    assert len(selected(compiled, "dds_prepare")) == 1
    assert len(selected(compiled, "dds_ram_start")) == 8


def test_800ns_xy_coexists_with_square_and_asf_replay():
    compiled = build_mixed(count=8, xy=True)
    assert verify_equivalence(compiled)["status"] == "passed"
    assert compiled.points[0].duration_mu == 6400


def test_unrelated_200ns_output_and_spi_preparation_can_cross_polar_events():
    qubits, lab = mixed_lab(asf=False)
    exp = Experiment("mixed_interior", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial():
            play(qubits[0].raman, envelope())
            play(qubits[0].raman, envelope(), phase=math.pi / 2)
        with exp.serial():
            delay(qubits[1].raman, 800 * ns)
            play(qubits[1].raman, square(200))
    compiled = lab.compile(exp)
    assert verify_equivalence(compiled)["status"] == "passed"
    assert (
        selected(compiled, "dds_effect")[0].start_mu - compiled.points[0].origin_mu
        == 800
    )


def test_hardware_rz_targets_polar_and_square_paths_together():
    (q, _), lab = mixed_lab(asf=False)
    lab.signal_map[q.rf] = "square"
    exp = Experiment("mixed_rz", [q])
    with exp.average(1), exp.realtime():
        with exp.parallel():
            with exp.serial(), exp.repeat(2):
                play(q.raman, envelope())
            with exp.serial(), exp.repeat(2):
                play(q.rf, square())
        rz(q, math.pi / 3)
    compiled = lab.compile(exp)
    assert verify_equivalence(compiled)["status"] == "passed"
    assert selected(compiled, "dds_polar_effect")[-1].params["polar_frame_only"]
    assert (
        selected(compiled, "dds_effect")[-1].start_mu - compiled.points[0].origin_mu
        == 1600
    )


def test_manifest_reports_both_resident_libraries(tmp_path):
    compiled = build_mixed(count=3, xy=True)
    artifact = compiled.export_artiq(tmp_path / "mixed")
    manifest = json.loads(artifact["manifest"].read_text())
    assert {bank["device"] for bank in manifest["ram_banks"]} == {"polar", "asf"}
    assert artifact["verification"]["polar_output"]["status"] == "passed"


def test_empty_sweep_point_keeps_valid_table_dispatch_and_host_order():
    qubits, lab = mixed_lab(asf=False)
    exp = Experiment("mixed_zero_repeat", qubits)
    count = SweepParameter("count", (0, 1, 2), "dimensionless")
    with exp.average(2), exp.sweep(count), exp.realtime(), exp.parallel():
        with exp.serial(), exp.repeat(count):
            play(qubits[0].raman, envelope())
        with exp.serial(), exp.repeat(count):
            play(qubits[1].raman, square())
    compiled = lab.compile(exp)
    assert verify_equivalence(compiled)["status"] == "passed"
    assert compiled.points[0].physical_events == []


def test_unknown_bus_cannot_hide_distinct_cpld_shadow_aliases():
    qubits, lab = mixed_lab(revision=9, asf=False)
    for name, device in lab.setup.devices.items():
        lab.setup.devices[name] = replace(device, bus=None)
    exp = Experiment("unknown_physical_topology", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        play(qubits[0].raman, envelope(200))
        play(qubits[1].raman, square(200))
    compiled = lab.compile(exp)
    source = generate_source(compiled)
    source = source.replace(
        "self.result_adapter = None",
        "self.result_adapter = None\n"
        "        self.device_1.bus.channel = self.device_0.bus.channel\n"
        "        self.device_1.chip_select = 5",
    )
    with pytest.raises(ValueError, match="CPLD configuration shadows"):
        verify_export(compiled, source=source)


def test_known_heterogeneous_cpld_protocols_are_not_silently_retargeted():
    qubits, lab = mixed_lab(asf=False)
    lab.setup.devices["square"] = replace(
        lab.setup.devices["square"], urukul_proto_rev=9
    )
    exp = Experiment("heterogeneous_cpld_compiler_limit", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        play(qubits[0].raman, envelope(200))
        play(qubits[1].raman, square(200))
    with pytest.raises(ValueError, match="mixed CPLD protocol revisions"):
        generate_source(lab.compile(exp))


def test_nonpolar_declared_protocol_is_checked_against_actual_driver():
    compiled = build_mixed()
    source = generate_source(compiled)
    source = source.replace(
        "self.result_adapter = None",
        "self.result_adapter = None\n        self.device_2.cpld.proto_rev = 9",
    )
    with pytest.raises(ValueError, match="declared hardware"):
        verify_export(compiled, source=source)


@pytest.mark.parametrize("alias", ["switch", "update"])
def test_rf_and_update_targets_cannot_alias_across_paths(alias):
    compiled = build_mixed()
    source = generate_source(compiled)
    rhs = "self.device_1.sw" if alias == "switch" else "self.device_1.io_update"
    source = source.replace(
        "self.result_adapter = None",
        "self.result_adapter = None\n"
        f"        self.device_2.sw.target_o = {rhs}.target_o",
    )
    with pytest.raises(ValueError, match="alias"):
        verify_export(compiled, source=source)


@pytest.mark.parametrize(
    "fault", ["square_write", "square_commit", "asf_launch", "asf_pow", "alias"]
)
def test_mixed_mock_rejects_corrupted_nonpolar_driver_operations(fault):
    compiled = build_mixed(count=3, xy=True)
    source = generate_source(compiled, mode="unrolled")
    if fault == "square_write":
        source = source.replace(
            "self.device_2.write64(0x15", "self.device_2.write64(0x14"
        )
    elif fault == "square_commit":
        # Remove only timed commits, leaving RF-off preload and epoch intact.
        event = selected(compiled, "dds_commit")[0]
        source = source.replace(
            f"at_mu(t0 + {event.start_mu})\n        self.device_2.io_update.on()",
            f"at_mu(t0 + {event.start_mu})\n        pass",
        )
    elif fault == "asf_launch":
        event = selected(compiled, "dds_ram_start")[0]
        source = source.replace(
            f"at_mu(t0 + {event.start_mu})\n        self.device_0.io_update.on()",
            f"at_mu(t0 + {event.start_mu})\n        pass",
        )
    elif fault == "asf_pow":
        source = source.replace("self.device_0.set_pow(", "self.device_0.set_pow(1 + ")
    else:
        source = source.replace(
            "self.result_adapter = None",
            "self.result_adapter = None\n        self.device_2.io_update.target_o "
            "= self.device_1.io_update.target_o",
        )
    with pytest.raises(ValueError):
        verify_export(compiled, source=source)
