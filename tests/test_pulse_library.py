"""Public pulse construction, byte snapshots and compact source IR."""

import base64
import copy
import hashlib
import json
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest

from taiyidsl import (
    Experiment,
    FlatTopGaussian,
    Gaussian,
    GaussianSquare,
    HalfSinePhase,
    Pulse,
    PulseLibrary,
    Qubit,
    SampledPulse,
    Square,
    ns,
    play,
    us,
)
from taiyidsl.dsl import json_value


def test_custom_factories_are_local_and_preserve_synthesized_data():
    left, right = PulseLibrary(), PulseLibrary()
    values = [0.25, 0.5]
    left.register(
        "drive",
        lambda *, dt: Pulse.from_array(values, kind="envelope", sample_interval=dt),
    )
    right.register("drive", Square)
    sampled = left.make("drive", dt=0.8 * ns)
    assert isinstance(sampled, SampledPulse)
    assert isinstance(right.make("drive", 10 * ns), Pulse)
    with pytest.raises(ValueError, match="already registered"):
        left.register("drive", Square)
    with pytest.raises(TypeError, match="callable"):
        left.register("invalid", sampled)
    left.register("invalid", lambda: values)
    with pytest.raises(TypeError, match="return a Pulse"):
        left.make("invalid")
    with pytest.raises(KeyError):
        right.make("unknown")


def test_aliases_preserve_existing_gaussian_shape_and_ir():
    assert GaussianSquare is FlatTopGaussian
    old = FlatTopGaussian(0, 8 * ns, cutoff_sigma=3, amplitude=0.2, sample_interval=ns)
    gaussian = Gaussian(sigma=8 * ns, cutoff_sigma=3, amplitude=0.2, sample_interval=ns)
    assert isinstance(gaussian, Pulse)
    assert json_value(gaussian) == json_value(old)
    assert gaussian == old


def test_samples_are_immutable_endian_independent_and_shared_by_play_overrides():
    backing = np.array([0.25, 0, -0.5, 1], dtype=">f4")
    pulse = Pulse.from_array(backing[::2], sample_interval=0.8 * ns, kind="envelope")
    raw = np.array([0.25, -0.5], dtype="<f8").tobytes()
    assert pulse.sample_asset() == {
        "count": 2,
        "dtype": "<f8",
        "data": base64.b64encode(raw).decode("ascii"),
    }
    assert pulse.sample_digest == hashlib.sha256(b"<f8" + raw).hexdigest()
    assert pulse.duration.value == Fraction(16, 10**10)
    backing[:] = 0
    np.testing.assert_array_equal(pulse.samples, [0.25, -0.5])
    with pytest.raises(ValueError):
        pulse.samples.flags.writeable = True
    assert pulse.samples is not pulse.samples
    assert pulse.samples.shape == (2,)
    assert replace(pulse, amplitude=0.5).samples.base is pulse.samples.base
    assert copy.deepcopy(pulse).samples.base is pulse.samples.base

    q = Qubit("q")
    exp = Experiment("sample", [q])
    with exp.average(1), exp.realtime():
        play(q.rf, pulse, amplitude=0.5)
    node = exp.root.children[0].children[0].children[0]
    emitted = node.params["pulse"]
    assert emitted.amplitude == 0.5 and pulse.amplitude is None
    assert emitted.sample_digest == pulse.sample_digest
    ir = json_value(exp.root)
    sample_ir = ir["children"][0]["children"][0]["children"][0]["params"]["pulse"]
    assert sample_ir["sample_interval_s"] == {
        "numerator": 1,
        "denominator": 1_250_000_000,
    }
    assert (sample_ir["sample_digest"], sample_ir["sample_count"]) == (
        pulse.sample_digest,
        2,
    )
    assert pulse.sample_asset()["data"] not in json.dumps(ir)
    assert (
        pulse.sample_digest
        != Pulse.from_array(
            [0.5, -0.25], sample_interval=0.8 * ns, kind="envelope"
        ).sample_digest
    )


def test_iq_snapshot_and_controls():
    values = np.array([0.2 + 0.3j, -1j], dtype=">c8")
    pulse = Pulse.from_array(
        values,
        sample_interval=ns,
        kind="iq",
        resample="linear",
        frequency=12e6,
        phase=0.5,
        phase_modulation=HalfSinePhase(0.25),
    )
    assert pulse.sample_asset()["dtype"] == "<c16"
    np.testing.assert_array_equal(pulse.samples, values)
    assert replace(pulse, phase_modulation=None).samples.base is pulse.samples.base
    assert json_value(pulse)["phase_modulation"]["kind"] == "half_sine"


def test_unit_iq_phasors_retain_their_original_components():
    source = np.exp(1j * np.linspace(0, 2 * np.pi, 513))
    pulse = Pulse.from_array(source, sample_interval=ns, kind="iq")
    np.testing.assert_array_equal(pulse.samples, source)


@pytest.mark.parametrize(
    ("samples", "kind", "error"),
    [
        ([], "envelope", ValueError),
        ([[0.5]], "waveform", ValueError),
        ([1, False], "iq", TypeError),
        (np.array([False]), "envelope", TypeError),
        (np.array([1], dtype=object), "iq", TypeError),
        (["0.5"], "iq", TypeError),
        ([1 + 0j], "waveform", TypeError),
        ([0.1 + 0j], "envelope", TypeError),
        ([np.nan], "iq", ValueError),
        ([np.inf], "envelope", ValueError),
        ([1 + 1j], "iq", ValueError),
        ([1.01], "waveform", ValueError),
        ([0], "complex", ValueError),
    ],
)
def test_invalid_samples(samples, kind, error):
    with pytest.raises(error):
        Pulse.from_array(samples, kind=kind, sample_interval=ns)


def test_sample_grid_and_raw_waveform_controls():
    with pytest.raises(TypeError, match="sample_interval"):
        Pulse.from_array([1], kind="waveform")
    with pytest.raises(ValueError, match="positive"):
        Pulse.from_array([1], sample_interval=0, kind="waveform")
    with pytest.raises(ValueError, match="resample"):
        Pulse.from_array([1], sample_interval=ns, kind="waveform", resample="nearest")
    for arg in ("frequency", "phase"):
        with pytest.raises(ValueError, match="waveform"):
            Pulse.from_array([1], kind="waveform", sample_interval=ns, **{arg: 0})
    raw = Pulse.from_array([0.25, -0.75], sample_interval=ns, kind="waveform")
    assert (raw.amplitude, raw.frequency, raw.phase) == (1, 0, 0)
    with pytest.raises(ValueError, match="phase controls"):
        Pulse.from_array(
            [1],
            sample_interval=ns,
            kind="waveform",
            phase_modulation=HalfSinePhase(0.3),
        )
    q = Qubit("q")
    exp = Experiment("raw", [q])
    with exp.average(1), exp.realtime():
        for arg in ("frequency", "detuning", "phase"):
            with pytest.raises(ValueError, match="waveform"):
                play(q.rf, raw, **{arg: 0})
        for arg in ("duration", "flat_duration"):
            with pytest.raises(TypeError, match="duration"):
                play(q.rf, raw, **{arg: 3 * us})
        play(q.rf, raw, amplitude=0.5)
