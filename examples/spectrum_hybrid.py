"""Offline ARTIQ + two M4i.6631 AWG examples; never open an instrument.

SIM_* serials, clock/trigger timing, and amplitudes are illustrative contracts.
The short random phase words test scheduling, not statistical RB or gate fidelity.
"""

import argparse
import json
import math
import random
from pathlib import Path

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    HalfSinePhase,
    LabConfig,
    PolynomialPhase,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    SweepParameter,
    delay,
    ms,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
    us,
)


def offline_lab(*, max_tile_samples=500000):
    """A uses only channel 0; unused A:1 is disabled. B uses both outputs."""
    devices = {
        "cooling_dds": Device(
            "cooling_dds", "dds", bus="spi0", update_group="urukul0", urukul_proto_rev=9
        ),
        "awg_trigger_a": Device("awg_trigger_a", "ttl", grid_mu=1),
        "awg_trigger_b": Device("awg_trigger_b", "ttl", grid_mu=1),
        "readout_ttl": Device("readout_ttl", "ttl", grid_mu=1),
        **{
            name: Device(name, "spectrum")
            for name in ("nuclear_rf", "clock_rf", "rydberg_rf")
        },
    }
    group = SpectrumGroup(
        trigger_devices={"A": "awg_trigger_a", "B": "awg_trigger_b"},
        cards={
            "A": SpectrumCard("SIM_NUCLEAR", (0,)),
            "B": SpectrumCard("SIM_AOM", (0, 1)),
        },
        outputs={
            "nuclear_rf": SpectrumOutput("A", 0),
            "clock_rf": SpectrumOutput("B", 0),
            "rydberg_rf": SpectrumOutput("B", 1),
        },
        max_tile_samples=max_tile_samples,
        calibration={"status": "unmeasured", "revision": "offline-example"},
    )
    setup = DeviceSetup(
        devices,
        name="spectrum-hybrid-offline-unmeasured",
        spectrum=SpectrumConfig(
            {"qubit_awg": group}, signal_frames={"clock": None, "rydberg": None}
        ),
    )
    mapping = SignalMap(
        {
            "q.rf": "nuclear_rf",
            "clock": "clock_rf",
            "rydberg": "rydberg_rf",
            "cooling": "cooling_dds",
            "readout": "readout_ttl",
        }
    )
    return LabConfig(setup, mapping, "artiq_spectrum")


def pulse_templates():
    """All Gaussian durations are TOTAL durations; sigma=T/20, cutoff=4."""
    return {
        "nuclear_square": Square(200 * us, amplitude=0.4, frequency=30000),
        "nuclear_shaped": FlatTopGaussian(
            120 * us,
            10 * us,
            cutoff_sigma=4,
            amplitude=0.4,
            frequency=30000,
            sample_interval=0.8 * ns,
        ),
        "clock": FlatTopGaussian(
            180 * us,
            15 * us,
            cutoff_sigma=4,
            amplitude=0.35,
            frequency=150e6,
            sample_interval=0.8 * ns,
            phase_modulation=HalfSinePhase(math.pi),
        ),
        "rydberg": FlatTopGaussian(
            240 * ns,
            20 * ns,
            cutoff_sigma=4,
            amplitude=0.3,
            frequency=200e6,
            sample_interval=0.8 * ns,
            phase_modulation=PolynomialPhase((0, 0, 3 * math.pi, -2 * math.pi)),
        ),
    }


def _experiment(name, **metadata):
    q = Qubit("q")
    exp = Experiment(
        name,
        [q],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "timing_calibration": "unmeasured",
            **metadata,
        },
    )
    return q, exp


def _cooling():
    # A native ARTIQ DDS operation, not a laboratory MOT/cooling recipe.
    play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))


def _readout():
    # Schedule only the TTL window; no camera result or physical measurement.
    play("readout", Square(10 * us))


def build_three_channel():
    q, exp = _experiment("three_channel_shapes")
    pulses = pulse_templates()
    with exp.average(1), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"):
            play(q.rf, pulses["nuclear_shaped"])
            with exp.parallel():
                play("clock", pulses["clock"])
                with exp.serial():
                    delay(100 * us)
                    play("rydberg", pulses["rydberg"])
        _readout()
    return offline_lab().compile(exp)


def build_rabi(*, amplitudes=(0.1, 0.25, 0.4, 0.55)):
    q, exp = _experiment("nuclear_rabi_amplitude", analysis="requires measured data")
    amplitude = SweepParameter("amplitude", amplitudes, "")
    with exp.average(2), exp.sweep(amplitude), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"):
            play(q.rf, pulse_templates()["nuclear_square"], amplitude=amplitude)
        _readout()
    return offline_lab().compile(exp)


def build_ramsey(*, delays_ms=(0.01, 1, 100)):
    q, exp = _experiment("nuclear_ramsey_delay", analysis="requires measured data")
    wait = SweepParameter("dark_time", delays_ms, "ms")
    pulse = pulse_templates()["nuclear_square"]
    with exp.average(2), exp.sweep(wait), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"):
            play(q.rf, pulse, amplitude=0.2)
            delay(wait)
            rz(q, math.pi / 2)
            play(q.rf, pulse, amplitude=0.2)
        _readout()
    return offline_lab().compile(exp)


def build_echo(*, half_wait_ms=1):
    q, exp = _experiment("nuclear_echo_timing", rotations="illustrative, uncalibrated")
    pulse = pulse_templates()["nuclear_square"]
    with exp.average(1), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"):
            play(q.rf, pulse, amplitude=0.2)
            delay(half_wait_ms * ms)
            play(q.rf, pulse, amplitude=0.4, phase=math.pi / 2)
            delay(half_wait_ms * ms)
            play(q.rf, pulse, amplitude=0.2)
        _readout()
    return offline_lab().compile(exp)


def build_virtual_z():
    q, exp = _experiment("nuclear_virtual_z_and_terminal_frame")
    pulse = pulse_templates()["nuclear_shaped"]
    with exp.average(1), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"):
            shift_phase(q.frame, 0.2)
            play(q.rf, pulse)
            rz(q, math.pi / 2)
            play(q.rf, pulse)
            reset_frame(q.frame)
            rz(q, math.pi / 3)  # Retained even with no subsequent RF pulse.
        _readout()
    return offline_lab().compile(exp)


def build_parallel():
    _, exp = _experiment("clock_rydberg_parallel")
    pulses = pulse_templates()
    with exp.average(1), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"), exp.parallel():
            play("clock", pulses["clock"])
            with exp.serial():
                delay(100 * us)
                play("rydberg", pulses["rydberg"])
        _readout()
    return offline_lab().compile(exp)


def build_stress(*, count=100, duration_ns=400, nuclear=False, seed=171):
    if type(count) is not int or count <= 0:
        raise ValueError("count must be a positive integer")
    q, exp = _experiment(
        f"{'nuclear' if nuclear else 'short'}_phase_words_{count}",
        gate_count=count,
        statistical_rb=False,
        inversion_gate=False,
        seed=seed,
        requested_intergate_gap_s=0,
    )
    rng = random.Random(seed)
    phases = [
        rng.choice((0, math.pi / 2, math.pi, 3 * math.pi / 2)) for _ in range(count)
    ]
    pulse = (
        pulse_templates()["nuclear_square"]
        if nuclear
        else Square(duration_ns * ns, amplitude=0.3, frequency=200e6)
    )
    with exp.average(1), exp.realtime():
        _cooling()
        with exp.triggered_sequence(group="qubit_awg"):
            for phase in phases:
                play(q.rf if nuclear else "rydberg", pulse, phase=phase)
        _readout()
    # Small tiles expose repeated segment reuse in the fast default examples.
    return offline_lab(max_tile_samples=500000 if nuclear else 4000).compile(exp)


def nuclear_capacity_prediction(count):
    """Conservative no-dedup byte bound; does not generate or validate samples."""
    group = offline_lab().setup.spectrum.groups["qubit_awg"]
    contract = group.cards["A"]
    samples_per_gate = 250000  # 200 us at 1.25 GS/s.
    prefix = group.trigger_lead_mu * contract.sample_rate_hz // 10**9
    prefix -= contract.trigger_latency_samples
    physical = ((prefix + count * samples_per_gate) // 32 + 1) * 32
    maximum = group.max_tile_samples
    segments = (physical + maximum - 1) // maximum
    slots = 1 << max(1, (segments - 1).bit_length())
    width = 2 * len(contract.active_channels)
    required = slots * maximum * width
    return {
        "gate_count": count,
        "gate_duration_s": 200e-6,
        "status": "analytic_upper_bound_not_a_compiled_program",
        "phase_aware_dedup_assumed": False,
        "A": {
            "physical_samples_upper_bound": physical,
            "raw_walltime_bytes": physical * width,
            "segments_upper_bound": segments,
            "segment_slots_upper_bound": slots,
            "resident_bytes_upper_bound": segments * maximum * width,
            "equal_partition_required_bytes_upper_bound": required,
            "available_bytes": contract.memory_bytes,
            "fits_memory_bound": required <= contract.memory_bytes,
            "fits_step_bound": segments <= 4096,
        },
        "B": {
            "output": "all zero",
            "zero_loop_samples": 4000,
            "resident_bytes_upper_bound": 3 * 8000 * 4,
            "equal_partition_required_bytes_upper_bound": 4 * 8000 * 4,
        },
    }


def acceptance_cases():
    return {
        "three_channel": build_three_channel,
        "rabi_amplitude": build_rabi,
        "ramsey_delay": build_ramsey,
        "echo": build_echo,
        "virtual_z": build_virtual_z,
        "parallel": build_parallel,
        "rb_like_100": lambda: build_stress(count=100),
        "rb_like_500": lambda: build_stress(count=500),
    }


def _point_summary(compiled):
    points = []
    for point in compiled.points:
        groups = [
            e.params for e in point.physical_events if e.kind == "spectrum_program"
        ]
        cards = {}
        for group in groups:
            for card_id, entry in group["cards"].items():
                program = entry["program"]
                gaps = []
                for channel in program["card_spec"]["active_channels"]:
                    pulses = [p for p in program["pulses"] if p["channel"] == channel]
                    gaps += [
                        b["start_sample"] - a["start_sample"] - a["duration_samples"]
                        for a, b in zip(pulses, pulses[1:])
                    ]
                cards[card_id] = {
                    "pulses": len(program["pulses"]),
                    "segments": len(program["segments"]),
                    "steps": len(program["steps"]),
                    "physical_duration_samples": program["physical_duration_samples"],
                    "raw_walltime_bytes": program["physical_duration_samples"]
                    * 2
                    * len(program["card_spec"]["active_channels"]),
                    "memory": program["memory"],
                    "min_intergate_gap_samples": min(gaps, default=None),
                    "max_intergate_gap_samples": max(gaps, default=None),
                }
        points.append(
            {
                "index": point.index,
                "coordinates": point.coordinates,
                "synchronous_launches": len(groups),
                "trigger_pulses": sum(len(group["trigger_routes"]) for group in groups),
                "cards": cards,
                "virtual_z_frames": compiled.virtual_z_frames(point.index),
            }
        )
    return points


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=(Path(__file__).resolve().parents[1] / "output" / "spectrum_hybrid"),
    )
    parser.add_argument("--case", action="append", choices=tuple(acceptance_cases()))
    parser.add_argument(
        "--nuclear-stress",
        action="store_true",
        help="Also materialize 100/500 x 200 us nuclear gate programs",
    )
    args = parser.parse_args(argv)
    cases = acceptance_cases()
    selected = list(cases) if args.case is None else list(dict.fromkeys(args.case))
    if args.nuclear_stress:
        for count in (100, 500):
            name = f"nuclear_stress_{count}"
            cases[name] = lambda count=count: build_stress(count=count, nuclear=True)
            selected.append(name)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "offline_only": True,
        "hardware": "not_verified",
        "artiq_compile": "not_run_by_this_example",
        "spectrum_driver": "not_run",
        "calibration": "unmeasured",
        "statistical_rb": False,
        "cases": [],
        "nuclear_capacity_predictions": [
            nuclear_capacity_prediction(n) for n in (100, 500)
        ],
    }
    for name in selected:
        print(f"Compiling and exporting {name} (offline)", flush=True)
        compiled = cases[name]()
        exported = compiled.export(output / name)
        report["cases"].append(
            {
                "case": name,
                "points": _point_summary(compiled),
                "verification": exported["verification"],
                "manifest": str(exported["manifest"]),
                "source": str(exported["source"]),
                "sequence_report": str(exported["report"]),
            }
        )
    summary = output / "summary.json"
    summary.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Saved {len(report['cases'])} offline cases: {summary}")
    return report


if __name__ == "__main__":
    main()
