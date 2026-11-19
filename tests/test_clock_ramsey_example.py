"""One end-to-end contract for the two introductory Ramsey experiments."""

import math
from fractions import Fraction

import numpy as np
import pytest

from examples.artiq_spectrum.clock_ramsey import build as build_clock_ramsey
from examples.artiq_spectrum.ramsey_lab.qubits import q0
from examples.artiq_spectrum.simple_ramsey import build as build_ramsey
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.reporting.output_simulator import _card_views


def test_ramsey_dark_times_and_clock_phase_scan_use_one_cross_card_timeline():
    snapshot = q0.to_dict()
    for build, expected_points in ((build_ramsey, 3), (build_clock_ramsey, 5)):
        lab, exp = build()
        compiled = lab.compile(exp)
        clock_experiment = exp.name == "clock_ramsey"
        assert len(compiled.points) == expected_points
        coordinates = [point.coordinates for point in compiled.points]
        if clock_experiment:
            assert [p["second_phase"] for p in coordinates] == pytest.approx(
                np.deg2rad([0, 45, 90, 135, 180])
            )
        else:
            assert [p["tau"] for p in coordinates] == pytest.approx([1e-6, 10e-6, 1e-4])
        for point in compiled.points:
            views = list(_card_views(compiled, point))
            ordered = []
            for view in views:
                rate = Fraction(str(view["program"]["card_spec"]["sample_rate_hz"]))
                for pulse in view["program"]["pulses"]:
                    start = view["sample0"] + Fraction(pulse["start_sample"], rate)
                    end = start + Fraction(pulse["duration_samples"], rate)
                    ordered.append((start, end, view["card"], pulse))
            ordered.sort(key=lambda item: item[0])
            first, last = ordered[0], ordered[-1]
            assert first[2] == last[2] == "A"
            assert first[3]["frequency_hz"] == last[3]["frequency_hz"] == 31_000
            assert first[3]["phase_rad"] == pytest.approx(0)
            local_phase = point.coordinates.get("second_phase", 0)
            expected_phase = (
                2 * math.pi * 31_000 * float(last[0] - first[0]) + local_phase
            ) % (2 * math.pi)
            assert last[3]["phase_rad"] == pytest.approx(expected_phase)
            if clock_experiment:
                assert len(ordered) == 3
                middle = ordered[1]
                assert middle[2] == "B" and middle[3]["channel"] == 0
                assert middle[3]["frequency_hz"] == 200_000
                assert middle[1] - middle[0] == Fraction(200, 1_000_000)
                # Different card prefixes must still reconstruct contiguous gates.
                assert first[1] == middle[0] and middle[1] == last[0]
                assert last[1] - first[0] == Fraction(400, 1_000_000)
            else:
                assert len(ordered) == 2
                assert float(last[0] - first[1]) == pytest.approx(
                    point.coordinates["tau"]
                )
            # Check one complete final DAC replay per experiment; other scan
            # points above vary only phase/dark-time metadata already asserted.
            if point.index != expected_points - 1:
                continue
            for view in views:
                program = view["program"]
                codes = reconstruct_window(
                    program,
                    0,
                    program["physical_duration_samples"],
                    max_samples=750_000,
                )
                expected = np.zeros_like(codes)
                for pulse in program["pulses"]:
                    start, size = pulse["start_sample"], pulse["duration_samples"]
                    channel = program["card_spec"]["active_channels"].index(
                        pulse["channel"]
                    )
                    angles = (
                        pulse["phase_rad"]
                        + 2 * np.pi * pulse["frequency_hz"] * np.arange(size) / 1.25e9
                    )
                    expected[start : start + size, channel] = np.rint(
                        pulse["amplitude"] * 32767 * np.cos(angles)
                    )
                np.testing.assert_allclose(codes, expected, atol=1)
    assert q0.to_dict() == snapshot
