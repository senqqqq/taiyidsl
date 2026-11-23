"""An additive drive offset resolves before either backend calculates phase."""

from contextlib import nullcontext

import numpy as np
import pytest
from test_spectrum_backend import group_program
from test_spectrum_phase_modes import phase_lab

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    MHz,
    Qubit,
    SignalMap,
    Square,
    SweepParameter,
    delay,
    play,
    reset_phase,
    trigplay,
    us,
)
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window


def test_detuning_matches_explicit_frequency_in_both_backends_and_carrier_phase():
    q = Qubit("q", signals={"rf": "q.rf"}, parameters={"rf": {"frequency": 10 * MHz}})
    before = q.to_dict()
    default_pulse = Square(4 * us, amplitude=0.2)
    fixed_pulse = Square(4 * us, amplitude=0.2, frequency=12 * MHz)
    artiq = LabConfig(
        DeviceSetup(
            {
                "dds": Device(
                    "dds",
                    "dds",
                    bus="spi",
                    update_group="board",
                    square_latency_mu=80,
                )
            }
        ),
        SignalMap({q.rf: "dds"}),
    )

    def experiment(spectrum, shifted):
        exp = Experiment("detuning", [q])
        with exp.average(1), exp.realtime():
            with exp.triggered_sequence(group="g") if spectrum else nullcontext():
                reset_phase(q.frame)
                play(
                    q.rf,
                    default_pulse,
                    **({"detuning": 1 * MHz} if shifted else {"frequency": 11 * MHz}),
                )
                delay(q.rf, 1 * us)
                play(
                    q.rf,
                    fixed_pulse,
                    **({"detuning": 1 * MHz} if shifted else {"frequency": 13 * MHz}),
                )
                delay(q.rf, 2 * us)
                emit = trigplay if spectrum else play
                emit(
                    q.rf,
                    fixed_pulse,
                    **(
                        {"frequency": 13 * MHz, "detuning": 1 * MHz}
                        if shifted
                        else {"frequency": 14 * MHz}
                    ),
                )
        return exp

    for spectrum, profile in ((False, artiq), (True, phase_lab("continuous"))):
        shifted = experiment(spectrum, True)
        explicit = experiment(spectrum, False)
        a, b = profile.compile(shifted), profile.compile(explicit)
        for field in ("logical_events", "physical_events"):

            def project(compiled):
                return [
                    (
                        e.kind,
                        e.signal,
                        e.start_mu,
                        e.duration_mu,
                        {k: v for k, v in e.params.items() if k != "cards"},
                    )
                    for e in getattr(compiled.points[0], field)
                ]

            assert project(a) == project(b)
        assert [
            e.params["frequency"]
            for e in a.points[0].logical_events
            if e.kind == "play"
        ] == [11e6, 13e6, 14e6]
        if spectrum:
            first = group_program(a)["cards"]["A"]["program"]
            second = group_program(b)["cards"]["A"]["program"]
            end = max(
                p["start_sample"] + p["duration_samples"] for p in first["pulses"]
            )
            np.testing.assert_array_equal(
                reconstruct_window(first, 0, end), reconstruct_window(second, 0, end)
            )
    assert q.to_dict() == before
    assert default_pulse.frequency is None and fixed_pulse.frequency == 12 * MHz


def test_detuning_rejects_non_scalar_wrong_units_and_negative_result():
    q = Qubit("q", signals={"rf": "q.rf"}, parameters={"rf": {"frequency": 10 * MHz}})
    pulse = Square(4 * us, amplitude=0.2)
    for invalid in (1 * us, float("nan"), SweepParameter("offset", [1, 2], "MHz")):
        exp = Experiment("bad_detuning", [q])
        with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
            with pytest.raises((ValueError, TypeError)):
                play(q.rf, pulse, detuning=invalid)
    for offset, valid in ((-10 * MHz, True), (-11 * MHz, False)):
        exp = Experiment("signed_detuning", [q])
        with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
            play(q.rf, pulse, detuning=offset)
        if valid:
            compiled = phase_lab().compile(exp)
            event = next(
                e for e in compiled.points[0].logical_events if e.kind == "play"
            )
            assert event.params["frequency"] == 0
        else:
            with pytest.raises(CompileError, match="frequency nonnegative"):
                phase_lab().compile(exp)
