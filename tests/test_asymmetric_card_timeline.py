"""One independent DAC oracle for unequal cards, delays and segment boundaries."""

import math
from dataclasses import replace

import numpy as np
from test_spectrum_trigger_routes import dual_lab

from taiyidsl import Experiment, Qubit, Square, delay, ns, play
from taiyidsl.spectrum_waveforms import reconstruct_window


def test_asymmetric_cards_reconstruct_on_one_timeline():
    profile = dual_lab()
    group = profile.setup.spectrum.groups["g"]
    group = replace(
        group,
        cards={
            "A": replace(group.cards["A"], trigger_latency_samples=500),
            "B": replace(group.cards["B"], trigger_latency_samples=520),
        },
        max_tile_samples=768,
        calibration={
            "status": "unmeasured",
            "revision": "synthetic-delay-model-not-hardware",
            "channels": {
                "A:0": {"delay_ns": 1.6, "sample_shift_samples": -2},
                "B:0": {"delay_ns": -2.4, "sample_shift_samples": 3},
            },
        },
    )
    profile.setup.spectrum = replace(profile.setup.spectrum, groups={"g": group})
    q, other = Qubit("q"), Qubit("other")
    profile.signal_map[other.rf] = "clock"
    exp = Experiment("asymmetric_cards", [q, other])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.parallel():
            with exp.serial():
                play(q.rf, Square(100 * ns, frequency=10e6, amplitude=0.3, phase=0.2))
                delay(2300 * ns)
                play(q.rf, Square(120 * ns, frequency=10e6, amplitude=0.4, phase=0.7))
            with exp.serial():
                delay(40 * ns)
                play(
                    other.rf, Square(80 * ns, frequency=25e6, amplitude=0.2, phase=-0.3)
                )
                delay(2280 * ns)
                play(
                    other.rf, Square(120 * ns, frequency=25e6, amplitude=0.3, phase=0.1)
                )
    compiled = profile.compile(exp)
    point = compiled.points[0]
    program_event = next(
        e for e in point.physical_events if e.kind == "spectrum_program"
    )
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    routes = program_event.params["trigger_routes"]
    assert len({(r["relative_mu"], r["width_mu"]) for r in routes}) == 1
    expected = {
        "A": [(0, 125, 10e6, 0.3, 0.2), (3000, 150, 10e6, 0.4, 0.7)],
        "B": [(50, 100, 25e6, 0.2, -0.3), (3000, 150, 25e6, 0.3, 0.1)],
    }
    boundaries = {}
    for name, item in program_event.params["cards"].items():
        program, contract = item["program"], item["contract"]
        samples = reconstruct_window(program, 0, program["physical_duration_samples"])
        trigger_s = routes[0]["relative_mu"] * 1e-9
        sample_zero_s = trigger_s + contract["trigger_latency_samples"] / 1.25e9
        # Undo only the declared fixed path delay; never fit an alignment.
        delay_s = group.calibration["channels"][f"{name}:0"]["delay_ns"] * 1e-9
        origin = round((sample_zero_s + delay_s - region.start_mu * 0.2e-9) * 1.25e9)
        global_samples = np.arange(len(samples)) + origin
        oracle = np.zeros(len(samples))
        for start, length, frequency, amplitude, phase in expected[name]:
            active = (global_samples >= start) & (global_samples < start + length)
            oracle[active] = amplitude * np.cos(
                2 * math.pi * frequency * global_samples[active] / 1.25e9 + phase
            )
        oracle = np.rint(oracle * 32767).astype(np.int16)
        np.testing.assert_allclose(samples[:, 0], oracle, atol=1)
        if samples.shape[1] == 2:
            assert not np.any(samples[:, 1])
        minimum = 384 // samples.shape[1]
        assert all(
            s["sample_count"] >= minimum and s["sample_count"] % 32 == 0
            for s in program["segments"]
        )
        assert program["tail_samples"] > 0
        assert not np.any(samples[-program["tail_samples"] :])
        segments = {s["id"]: s for s in program["segments"]}
        boundaries[name] = [
            (segments[s["segment_id"]]["sample_count"], s["loops"])
            for s in program["steps"]
        ]
    assert boundaries["A"] != boundaries["B"]
