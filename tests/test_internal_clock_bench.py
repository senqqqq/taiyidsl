"""Internal PLL is a declared one-AWG bench contract, never a clock fallback."""

from dataclasses import replace

import pytest
from spectrum_sdk_double import FakeSdk
from test_nix_worker import build

from taiyidsl.config.spectrum import (
    INTERNAL_PLL_BENCH,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
)
from taiyidsl.ir import CompileError
from taiyidsl.reporting.spectrum_report import card_execution
from taiyidsl.runtime.spectrum.awg import (
    SpcmAdapter,
    SpectrumRunner,
    SpectrumRuntimeError,
)
from taiyidsl.spectrum_identity import hardware_fingerprint


def card_entry(compiled):
    return next(
        e.params["cards"]["A"]
        for e in compiled.points[0].physical_events
        if e.kind == "spectrum_program"
    )


def test_bench_contract_and_waveform_are_independent():
    _, strict = build("unused", False)
    lab, bench = build("unused", False, bench=True)
    config = lab.setup.spectrum
    assert SpectrumConfig.from_dict(config.to_dict()) == config
    a, b = card_entry(strict), card_entry(bench)
    assert a["contract"]["reference_clock_hz"] == 125_000_000
    assert "clock_policy" not in a["contract"]
    assert b["contract"]["reference_clock_hz"] is None
    assert b["contract"]["clock_policy"] == INTERNAL_PLL_BENCH
    assert hardware_fingerprint(a["contract"]) != hardware_fingerprint(b["contract"])
    for key in ("pulses", "segments", "steps", "physical_duration_samples"):
        assert a["program"][key] == b["program"][key]

    def edges(point):
        return [
            (e.kind, e.signal, e.start_mu, e.duration_mu)
            for e in point.physical_events
            if e.kind.startswith("ttl_")
        ]

    assert edges(strict.points[0]) == edges(bench.points[0])
    rows = {r["setting"]: r["value"] for r in card_execution(b)["configuration"]}
    assert rows["clock.mode"] == "SPC_CM_INTPLL"
    assert rows["clock.reference_clock [Hz]"] is None
    assert rows["trigger.or_mask / and_mask"] == "SPC_TMASK_EXT0 / 0"
    with pytest.raises(CompileError, match="shot-wide"):
        build("unused", False, bench=True, phase_reference="shot")


def test_bench_rejects_incompatible_declarations():
    opts = dict(serial_number="101", clock_policy=INTERNAL_PLL_BENCH)
    for extra in ({"mode": "dds"}, {"reference_clock_hz": 125_000_000}):
        with pytest.raises(ValueError, match="without a reference"):
            SpectrumCard(**opts, **extra)
    with pytest.raises(ValueError, match="125 MHz"):
        SpectrumCard("101", reference_clock_hz=10_000_000)
    with pytest.raises(ValueError, match="Unknown Spectrum clock_policy"):
        SpectrumCard("101", clock_policy="any")
    group = SpectrumGroup(
        "ttl20", {"A": SpectrumCard(**opts)}, {"out": SpectrumOutput("A", 0)}
    )
    for profile in (
        {"status": "measured"},
        {"status": "unmeasured", "channels": {"A:0": {"gain": 2}}},
    ):
        with pytest.raises(ValueError, match="uncorrected setpoints"):
            replace(group, calibration=profile)
    with pytest.raises(ValueError, match="one Spectrum card"):
        replace(group, cards={**group.cards, "B": SpectrumCard("102")})
    with pytest.raises(ValueError, match="another Spectrum group"):
        SpectrumConfig({"one": group, "two": group})


@pytest.mark.parametrize(
    "mode,rate",
    [("external", 1_250_000_000), ("other", 1_250_000_000), ("internal", 625_000_000)],
)
def test_wrong_clock_never_sets_up_or_switches(mode, rate):
    _, compiled = build("unused", False, bench=True)
    spec = card_entry(compiled)["contract"]
    sdk = FakeSdk()
    sdk.clock.update(
        mode={
            "external": sdk.SPC_CM_EXTREFCLOCK,
            "other": -1,
            "internal": sdk.SPC_CM_INTPLL,
        }[mode],
        sample_rate=rate,
    )
    adapter = SpcmAdapter("101", sdk=sdk)
    with pytest.raises(SpectrumRuntimeError, match="expected.*internal_pll.*read back"):
        adapter.configure(spec)
    with pytest.raises(SpectrumRuntimeError, match="not for internal PLL"):
        adapter.configure_for_calibration(spec)
    assert sdk.log == [("open",)]


def test_external_default_does_not_fall_back_to_internal_pll():
    _, compiled = build("unused", False)
    sdk = FakeSdk()
    # A stale 125 MHz register does not make the card externally referenced.
    sdk.clock["mode"] = sdk.SPC_CM_INTPLL
    adapter = SpcmAdapter("101", sdk=sdk)
    with pytest.raises(SpectrumRuntimeError, match="external_reference.*internal_pll"):
        adapter.configure(card_entry(compiled)["contract"])
    assert sdk.log == [("open",)]


def test_point_runner_checks_bench_profile_hash_before_setup():
    _, compiled = build("unused", False, bench=True)
    entry = card_entry(compiled)
    sdk = FakeSdk()
    sdk.clock["mode"] = sdk.SPC_CM_INTPLL
    adapter = SpcmAdapter("101", sdk=sdk)
    profile = {"status": "unmeasured", "revision": "unmeasured"}
    group = {"cards": {"A": entry}, "calibration": profile}
    manifest = {"schema_version": 1, "points": [{"index": 0, "groups": [group]}]}
    runner = SpectrumRunner({"A": adapter})
    profile["revision"] = "different"
    with pytest.raises(SpectrumRuntimeError, match="fingerprint"):
        runner.run_point(manifest, 0, lambda _: pytest.fail("unexpected TTL"))
    profile["revision"] = "unmeasured"
    result = runner.run_point(manifest, 0, lambda _: sdk.log.append(("ttl",)))
    assert result["cards"]["A"]["output_basis"] == "setpoints_only"
    assert ("trigger", "or_mask", sdk.SPC_TMASK_EXT0) in sdk.log
    events = [e[0] for e in sdk.log]
    assert events.index("dma") < events.index("start") < events.index("ttl")
    assert "clock_write" not in events
