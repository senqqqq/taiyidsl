"""Output semantics across the prepare/commit/effect backend migration.

Frozen logical template traces are checked in test_template_equivalence.py.
"""

import math

import pytest

from taiyidsl import (
    Qubit,
    compile_experiment,
    rabi,
    ramsey,
    randomized_benchmarking,
    reference_setup,
    us,
)
from taiyidsl.backends.artiq.verification import verify_equivalence


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
def test_reference_templates_preserve_output_states_and_host_order(mode):
    q = Qubit("q0")
    experiments = [
        rabi(q, [0 * us, 4 * us, 8 * us], shots=2, mode=mode),
        ramsey(q, [0 * us, 4 * us, 8 * us], shots=2, mode=mode),
        randomized_benchmarking(
            q,
            [0, 1, 3],
            sequence_count=2,
            shots=2,
            seed=7,
            inter_gate_delay=2 * us,
            mode=mode,
        ),
    ]
    for experiment in experiments:
        compiled = compile_experiment(experiment, *reference_setup([q]))
        for point in compiled.points:
            for play in (e for e in point.logical_events if e.kind == "play"):
                if play.duration_mu == 0:
                    continue
                name = compiled.signal_map[play.signal]
                effects = [
                    e
                    for e in point.physical_events
                    if e.kind == "dds_effect"
                    and e.signal == name
                    and e.start_mu - point.origin_mu <= play.start_mu
                ]
                assert effects, (experiment.name, point.index, play.start_mu)
                state = effects[-1].params
                for key in ("frequency", "amplitude"):
                    assert state[key] == pytest.approx(play.params[key])
                assert state["phase"] == pytest.approx(
                    play.params["phase"] % (2 * math.pi)
                )
                # Adjacent gates may share one continuous RF-on interval.
                edges = [
                    e
                    for e in point.physical_events
                    if e.signal == name and e.kind in {"dds_on", "dds_off"}
                ]
                assert any(
                    on.start_mu - point.origin_mu <= play.start_mu
                    and off.start_mu - point.origin_mu >= play.end_mu
                    for on, off in zip(edges[::2], edges[1::2])
                )
        expected = (
            [(p, s) for s in range(2) for p in range(len(compiled.points))]
            if mode == "cyclic"
            else [(p, s) for p in range(len(compiled.points)) for s in range(2)]
        )
        assert list(compiled.execution_order()) == expected
        assert verify_equivalence(compiled)["status"] == "passed"
