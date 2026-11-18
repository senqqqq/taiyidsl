"""Independent SPI preparation, strict commits and actual SED submission order."""

import math
from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    delay,
    ns,
    play,
)
from taiyidsl.artiq import verify_equivalence
from taiyidsl.backends._preparation import ConfigurationRequest, place_configurations
from taiyidsl.ir import ScheduledEvent
from taiyidsl.submission import (
    ordered_rtio_events,
    preparation_window,
    validate_channel_order,
    validate_sed,
    validate_submission_plan,
)


def setup_for(*, same_bus=False, unknown=False):
    return DeviceSetup(
        {
            name: Device(
                name,
                "dds",
                bus=None if unknown else f"spi{0 if same_bus else index}",
                update_group=f"board{index}",
                ram_phase_update_duration_mu=480,
                ram_latency_mu=112,
            )
            for index, name in enumerate(("a", "b"))
        }
        | {"ttl": Device("ttl", "ttl", grid_mu=1, min_duration_mu=1)}
    )


def request(name, *, deadline=800, earliest=0, kind="dds_ram_phase"):
    event = ScheduledEvent(
        kind,
        name,
        deadline,
        480,
        {"ram_boundary": True, "effect_mu": deadline + 112, "ram_latency_mu": 112},
        resources=(name, "kernel_submit"),
    )
    return ConfigurationRequest(event, earliest)


def place(requests, setup, *, edges=(), jobs=(), reservations=()):
    return place_configurations(requests, [], setup, edges, jobs, reservations)[0]


def commits(name, timestamp=800):
    return [
        ScheduledEvent("dds_ram_start", name, timestamp, 0, {"ram_boundary": True}),
        ScheduledEvent("dds_ram_commit_off", name, timestamp + 8, 0),
    ]


def ordered(events):
    return sorted(events, key=lambda event: event.start_mu)


def test_two_independent_800ns_prepare_windows_really_overlap():
    setup = setup_for()
    prepared = place([request("a"), request("b")], setup)
    assert [(event.start_mu, event.end_mu) for event in prepared] == [(320, 800)] * 2
    events = ordered(prepared + commits("a") + commits("b"))
    validate_submission_plan(events, setup)
    assert [timestamp for timestamp, _ in ordered_rtio_events(events, setup)][:4] == [
        320,
        328,
        320,
        328,
    ]


@pytest.mark.parametrize("options", [{"same_bus": True}, {"unknown": True}])
def test_same_or_unknown_bus_cannot_claim_independent_bandwidth(options):
    with pytest.raises(CompileError, match="preparation window"):
        place([request("a"), request("b")], setup_for(**options))


def test_unrelated_200ns_output_can_occur_during_both_transfers():
    setup = setup_for()
    prepared = place([request("a"), request("b")], setup, edges=(520, 720))
    assert [event.start_mu for event in prepared] == [320, 320]
    ttl = [
        ScheduledEvent("ttl_on", "ttl", 520, 0),
        ScheduledEvent("ttl_off", "ttl", 720, 0),
    ]
    validate_submission_plan(
        ordered(prepared + commits("a") + commits("b") + ttl), setup
    )


def test_opaque_legacy_call_still_cannot_cross_output_edge():
    setup = setup_for()
    prepared = place([request("a", kind="dds_config")], setup, edges=(520,))
    assert (prepared[0].start_mu, prepared[0].end_mu) == (40, 520)


def test_fixed_preparation_window_preserves_bus_independence():
    setup = setup_for()
    a = place([request("a")], setup)[0]
    window = preparation_window(a, setup)
    b = place([request("b")], setup, jobs=[window])[0]
    assert a.start_mu == b.start_mu == 320
    # A plain legacy tuple has no certified buffering information.
    with pytest.raises(CompileError, match="preparation window"):
        place([request("b")], setup, jobs=[tuple(window)])


def test_explicit_kernel_submission_reservation_still_blocks_buffered_spi():
    with pytest.raises(CompileError, match="preparation window"):
        place(
            [request("a"), request("b")],
            setup_for(),
            reservations=[(400, 600, ("kernel_submit",), "reserved")],
        )


def test_validator_catches_tampered_bus_overlap_and_early_commit():
    setup = setup_for()
    prepared = place([request("a"), request("b")], setup)
    setup.devices["b"] = replace(setup.devices["b"], bus="spi0")
    with pytest.raises(CompileError, match="SPI bus"):
        validate_submission_plan(ordered(prepared + commits("a") + commits("b")), setup)
    with pytest.raises(CompileError, match="before buffered SPI data is ready"):
        validate_submission_plan(
            ordered([prepared[0]] + commits("a", 720)), setup_for()
        )


def test_pending_state_cannot_be_overwritten_before_commit():
    setup = setup_for()
    first = request("a", deadline=1000).event
    first.start_mu = 0
    second = request("a", deadline=1000).event
    second.start_mu = 480
    with pytest.raises(CompileError, match="overwritten"):
        validate_submission_plan(ordered([first, second] + commits("a", 1000)), setup)


def test_sed_checks_submission_order_not_just_same_timestamp_counts():
    timestamps = [
        timestamp for i in range(9) for timestamp in (1000 - 8 * i, 2000 - 8 * i)
    ]
    assert len({timestamp // 8 for timestamp in timestamps}) == len(timestamps)
    with pytest.raises(CompileError, match="SED lane capacity"):
        validate_sed(timestamps, coarse_mu=8, lane_count=8)
    validate_sed(sorted(timestamps), coarse_mu=8, lane_count=8)


def test_sed_known_same_time_limit_and_channel_collisions():
    validate_sed([1000] * 8, lane_count=8)
    with pytest.raises(CompileError, match="SED lane capacity"):
        validate_sed([1000] * 9, lane_count=8)
    with pytest.raises(CompileError, match="colliding"):
        validate_channel_order([(1000, "spi0"), (1004, "spi0")], coarse_mu=8)


def test_opaque_span_remains_barrier_in_validation():
    call = ScheduledEvent("dds_config", "a", 0, 3000)
    ttl = ScheduledEvent("ttl_on", "ttl", 200, 0)
    with pytest.raises(CompileError, match="opaque"):
        validate_submission_plan([call, ttl], setup_for())


def test_opaque_calls_with_equal_starts_cannot_evade_barrier():
    call = ScheduledEvent("dds_config", "a", 0, 3000)
    other = ScheduledEvent("dds_config", "b", 0, 3000)
    edge = ScheduledEvent("ttl_on", "ttl", 0, 0)
    with pytest.raises(CompileError, match="opaque"):
        validate_submission_plan([call, other], setup_for())
    with pytest.raises(CompileError, match="opaque"):
        validate_submission_plan([call, edge], setup_for())
    # A zero-duration edge submitted before the opaque call is well-defined.
    validate_submission_plan([edge, call], setup_for())


def continuous_lab(*, same_bus=False):
    """Synthetic matched latencies isolate SPI feasibility, not hardware proof."""
    qubits = [Qubit("q0"), Qubit("q1")]
    setup = setup_for(same_bus=same_bus)
    for name in ("a", "b"):
        setup.devices[name] = replace(
            setup.devices[name],
            nco_mode="continuous",
            nco_epoch_latency_mu=80,
            ram_pow_latency_mu=112,
            ram_rearm_duration_mu=4000,
            square_latency_mu=80,
        )
    return qubits, LabConfig(
        setup, SignalMap({q.raman: name for q, name in zip(qubits, ("a", "b"))})
    )


def ram800():
    return FlatTopGaussian(
        0 * ns,
        sigma=80 * ns,
        cutoff_sigma=5,
        sample_interval=4 * ns,
        frequency=83.123456e6,
        amplitude=0.5,
    )


def test_compiled_two_board_800ns_phase_preparation_overlaps_without_gap():
    qubits, lab = continuous_lab()
    exp = Experiment("two_spi_800ns", qubits)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    with exp.serial():
                        for phase in (0, math.pi / 2, math.pi):
                            play(q.raman, ram800(), phase=phase)
    compiled = lab.compile(exp)
    point = compiled.points[0]
    assert point.duration_mu == 2400
    writes = [e for e in point.physical_events if e.kind == "dds_ram_phase"]
    assert len(writes) == 4
    assert writes[0].start_mu == writes[1].start_mu
    assert writes[2].start_mu == writes[3].start_mu
    assert verify_equivalence(compiled)["status"] == "passed"
    lab.setup.devices["b"] = replace(lab.setup.devices["b"], bus="spi0")
    with pytest.raises(CompileError, match="preparation window"):
        lab.compile(exp)


def test_compiled_200ns_square_edges_during_other_ram_preparation():
    (q0, q1), lab = continuous_lab()
    exp = Experiment("single_200ns_during_prepare", [q0, q1])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(q0.raman, ram800())
                    play(q0.raman, ram800(), phase=math.pi / 2)
                with exp.serial():
                    delay(q1.raman, 400 * ns)
                    play(q1.raman, Square(200 * ns, frequency=83.123456e6))
    compiled = lab.compile(exp)
    point = compiled.points[0]
    write = next(e for e in point.physical_events if e.kind == "dds_ram_phase")
    edges = [
        e
        for e in point.physical_events
        if e.signal == "b" and e.kind in {"dds_on", "dds_off"}
    ]
    assert [e.start_mu - point.origin_mu for e in edges] == [400, 600]
    assert all(write.start_mu < e.start_mu < write.end_mu for e in edges)
    assert verify_equivalence(compiled)["status"] == "passed"
