"""Legacy preparation windows with a real-driver-compatible 3 us budget.

This preserves the original scheduler geometry scaled by three. Timed Square
has separate tests because it intentionally permits preparing during RF output.
"""

from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    Qubit,
    Square,
    compile_experiment,
    delay,
    play,
    reference_setup,
    reserve,
    us,
)
from taiyidsl.backends.artiq.verification import verify_equivalence

SLOT = 3 * us


def legacy_setup(qubits):
    setup, mapping = reference_setup(qubits)
    setup.devices = {
        name: replace(
            device,
            square_latency_mu=None,
            config_duration_mu=3000,
            nco_mode="legacy_tracking",
        )
        for name, device in setup.devices.items()
    }
    return setup, mapping


def parallel_sequence(
    ends=(4, 6), start=7, reverse=False, swap=False, reservation=None, edge=False
):
    qubits = [Qubit("q0"), Qubit("q1")]
    setup, mapping = legacy_setup(qubits)
    if swap:
        mapping[qubits[0].raman], mapping[qubits[1].raman] = (
            mapping[qubits[1].raman],
            mapping[qubits[0].raman],
        )
    experiment = Experiment("parallel_preparation", qubits)
    with experiment.average(1):
        with experiment.realtime():
            with experiment.parallel():
                if reservation is not None:
                    with experiment.serial():
                        delay(qubits[0].readout, 4 * SLOT)
                        reserve("kernel_submit", reservation * SLOT)
                if edge:
                    with experiment.serial():
                        delay(qubits[0].readout, 5.5 * SLOT)
                        play(
                            qubits[0].readout,
                            Square(0.1 * SLOT, amplitude=1, frequency=0),
                        )
                branches = list(zip(qubits, ends))
                for qubit, end in reversed(branches) if reverse else branches:
                    with experiment.serial():
                        play(qubit.raman, Square(end * SLOT))
                        delay(qubit.raman, (start - end) * SLOT)
                        play(qubit.raman, Square(4 * SLOT, phase=1))
    return compile_experiment(experiment, setup, mapping)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("swap", [False, True])
def test_parallel_preparation_is_not_decided_by_device_names(reverse, swap):
    compiled = parallel_sequence(reverse=reverse, swap=swap)
    point = compiled.points[0]
    assert sorted(
        (e.signal, e.start_mu, e.duration_mu)
        for e in point.logical_events
        if e.kind == "play"
    ) == [
        ("q0.raman", 0, 12000),
        ("q0.raman", 21000, 12000),
        ("q1.raman", 0, 18000),
        ("q1.raman", 21000, 12000),
    ]
    windows = {
        e.signal: (e.start_mu - point.origin_mu, e.end_mu - point.origin_mu)
        for e in point.physical_events
        if e.kind == "dds_config" and e.params["phase"] == 1
    }
    assert windows[compiled.signal_map["q0.raman"]] == (15000, 18000)
    assert windows[compiled.signal_map["q1.raman"]] == (18000, 21000)
    assert verify_equivalence(compiled)["status"] == "passed"


def test_parallel_preparation_still_rejects_insufficient_total_window():
    with pytest.raises(CompileError, match="preparation window"):
        parallel_sequence(ends=(6, 6), start=7)


def test_reordered_preparation_preserves_explicit_reservations():
    compiled = parallel_sequence(reservation=1)
    assert verify_equivalence(compiled)["status"] == "passed"
    with pytest.raises(CompileError, match="preparation window"):
        parallel_sequence(reservation=2)


def test_reordered_preparation_does_not_swallow_independent_ttl_edges():
    compiled = parallel_sequence(edge=True)
    point = compiled.points[0]
    config = next(
        e
        for e in point.physical_events
        if e.kind == "dds_config"
        and e.signal == compiled.signal_map["q0.raman"]
        and e.params["phase"] == 1
    )
    # Finish before the TTL pulse at 16.5 us, on the 8-mu configuration grid.
    assert (config.start_mu - point.origin_mu, config.end_mu - point.origin_mu) == (
        13496,
        16496,
    )
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("shared", [False, True])
def test_nested_shared_group_outputs_keep_the_long_interval_blocking(shared):
    a, b = Qubit("q0"), Qubit("q1")
    setup, mapping = legacy_setup([a, b])
    if shared:
        setup.devices[mapping[a.raman]] = replace(
            setup.devices[mapping[a.raman]], update_group="urukul1"
        )
    experiment = Experiment("nested_outputs", [a, b])
    with experiment.average(1):
        with experiment.realtime():
            with experiment.parallel():
                play(b.raman, Square(8 * SLOT))
                with experiment.serial():
                    delay(b.rf, 2 * SLOT)
                    play(b.rf, Square(1 * SLOT))
                with experiment.serial():
                    delay(a.raman, 8 * SLOT)
                    play(a.raman, Square(1 * SLOT))
    compiled = compile_experiment(experiment, setup, mapping)
    point = compiled.points[0]
    target = next(
        e
        for e in point.physical_events
        if e.kind == "dds_config" and e.signal == mapping[a.raman]
    )
    if shared:
        # The short 6--9 us output has ended, but the 0--24 us output still blocks.
        assert target.end_mu - point.origin_mu <= 0
    else:
        assert (target.start_mu - point.origin_mu, target.end_mu - point.origin_mu) == (
            21000,
            24000,
        )
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("bus", ["spi0", "spi1"])
def test_nested_reservations_across_resources_keep_long_relevant_span(bus):
    a, b = Qubit("q0"), Qubit("q1")
    setup, mapping = legacy_setup([a, b])
    experiment = Experiment("nested_reservations", [a, b])
    with experiment.average(1):
        with experiment.realtime():
            with experiment.parallel():
                reserve(bus, 8 * SLOT)
                with experiment.serial():
                    delay(b.readout, 2 * SLOT)
                    reserve("kernel_submit", 1 * SLOT)
                with experiment.serial():
                    delay(a.raman, 8 * SLOT)
                    play(a.raman, Square(1 * SLOT))
    compiled = compile_experiment(experiment, setup, mapping)
    point = compiled.points[0]
    config = next(e for e in point.physical_events if e.kind == "dds_config")
    assert config.end_mu - point.origin_mu == (0 if bus == "spi0" else 24000)
    assert verify_equivalence(compiled)["status"] == "passed"
