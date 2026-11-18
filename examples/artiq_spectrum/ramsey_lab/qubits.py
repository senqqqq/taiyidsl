"""Illustrative pulse parameters, not a physical calibration certificate."""

from taiyidsl import Qubit, kHz, us

q0 = Qubit(
    uid="q0",
    signals={"rf": "q0.rf", "clock": "q0.clock", "rydberg": "q0.rydberg"},
    parameters={
        "rf": {
            "frequency": 30 * kHz,
            "resonance_frequency": 30 * kHz,
            "pi_amplitude": 0.2,
            "pi2_amplitude": 0.2,
            "pi_duration": 200 * us,
            "pi2_duration": 100 * us,
            "waveform": "square",
            "frame": "spin",
        },
        "clock": {
            # Demo drive values only; optical resonance/calibration is not inferred.
            "frequency": 200 * kHz,
            "pi_amplitude": 0.2,
            "pi2_amplitude": 0.2,
            "pi_duration": 100 * us,
            "pi2_duration": 50 * us,
            "waveform": "square",
            "frame": "clock_control",
        },
        "rydberg": {},
        "extensions": {},
    },
    position=None,
)
