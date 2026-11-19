"""Frozen pre-refactor events for reusable tune-up pulse templates."""

import json
from pathlib import Path

import pytest
from optics_fingerprint import assert_logical_baseline

from taiyidsl import Qubit, compile_experiment, reference_setup, us
from taiyidsl.config.models import DriveParameters, QubitParameters, ReadoutParameters
from taiyidsl.experiments.templates import rabi, ramsey, randomized_benchmarking

BASELINE = Path(__file__).parent / "fixtures/pre_template_refactor_events.json"


def template_cases(mode):
    q = Qubit(
        "q0",
        QubitParameters(
            raman=DriveParameters(
                frequency=82e6,
                amplitude=0.4,
                pi_duration=12e-6,
                pi2_duration=7e-6,
            ),
            readout=ReadoutParameters(duration=15e-6),
        ),
    )
    return {
        "rabi": rabi(q, [0 * us, 4 * us, 12 * us], shots=3, mode=mode),
        "rabi_zero_amplitude": rabi(
            q, [0 * us, 4 * us, 12 * us], shots=3, mode=mode, amplitude=0
        ),
        "ramsey": ramsey(
            q, [0 * us, 4 * us, 12 * us], shots=3, mode=mode, detuning=-25e3
        ),
        "rb": randomized_benchmarking(
            q,
            [0, 1, 3],
            sequence_count=2,
            shots=3,
            mode=mode,
            seed=8,
            inter_gate_delay=2 * us,
        ),
    }


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
@pytest.mark.parametrize("name", ["rabi", "rabi_zero_amplitude", "ramsey", "rb"])
def test_template_events_match_pre_refactor_baseline(mode, name):
    experiment = template_cases(mode)[name]
    compiled = compile_experiment(
        experiment, *reference_setup(experiment.qubits.values())
    )
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert_logical_baseline(compiled, baseline[mode][name])
