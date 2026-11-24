import math

import numpy as np
import pytest

from taiyidsl import Pulse, load_iq_csv, ns
from taiyidsl.dsl import load_iq_csv as dsl_load_iq_csv


def test_direct_iq_keeps_sign_order_and_absolute_scale(tmp_path):
    path = tmp_path / "IQ.csv"
    path.write_text(
        'time,Q,I,note\n3,-0.5,0.25,"two, words"\n\n2,0.75,-0.125,test\n',
        encoding="utf-8-sig",
    )
    samples = load_iq_csv(path)
    assert dsl_load_iq_csv is load_iq_csv
    assert samples.dtype == np.complex128 and samples.shape == (2,)
    np.testing.assert_array_equal(samples, [0.25 - 0.5j, -0.125 + 0.75j])
    pulse = Pulse.from_array(samples, sample_interval=0.8 * ns, kind="iq")
    samples[0] = 0
    assert pulse.samples[0] == 0.25 - 0.5j


@pytest.mark.parametrize(
    ("phase_column", "phase_unit"),
    [
        (f"0\n0.5,{math.pi / 2}\n0.25,{-math.pi / 2}", "rad"),
        ("0\n0.5,90\n0.25,-90", "deg"),
    ],
)
def test_polar_is_cosine_referenced(tmp_path, phase_column, phase_unit):
    path = tmp_path / "polar.csv"
    path.write_text(f"A,phi\n1,{phase_column}\n", encoding="utf-8")
    iq = load_iq_csv(path, phase_unit=phase_unit)
    np.testing.assert_allclose(iq, [1, 0.5j, -0.25j], atol=1e-15)
    np.testing.assert_allclose((iq * 1j).real, [0, -0.5, 0.25], atol=1e-15)


def test_custom_columns_and_explicit_choice_of_representation(tmp_path):
    path = tmp_path / "both.csv"
    path.write_text("I,Q,A,phi,level,angle\n0.25,-0.25,1,90,-0.5,180\n")
    with pytest.raises(ValueError, match="Specify column names"):
        load_iq_csv(path)
    assert load_iq_csv(path, i="I", q="Q")[0] == 0.25 - 0.25j
    assert load_iq_csv(path, amplitude="level", phase="angle", phase_unit="deg")[
        0
    ] == pytest.approx(0.5)
    assert load_iq_csv(path, amplitude="A", phase="phi", phase_unit="deg")[
        0
    ] == pytest.approx(1j)


@pytest.mark.parametrize(
    ("body", "options", "error"),
    [
        ("", {}, "header"),
        ("I,Q\n\n", {}, "no sample rows"),
        ("X,Y\n0,0", {}, "Specify column names"),
        ("I,Q\n0,0", {"i": "I"}, "both column names"),
        ("I,Q\n0,0", {"i": "I", "q": "I"}, "distinct"),
        ("I,I,Q\n0,0,0", {}, "uniquely named"),
        ("I,Q\n0,0", {"i": "X", "q": "Q"}, "uniquely named"),
        ("I,Q\n0,0", {"i": "I", "q": "Q", "phase": "Q"}, "either"),
        ("I,Q\n0,0", {"phase_unit": "deg"}, "only to amplitude"),
        ("A,phi\n1,0", {"phase_unit": "degrees"}, "phase_unit"),
        ("I,Q\n0,0\n\n0,xxx", {}, "line 4: expected numbers"),
        ("I,Q\n0,0,1", {}, "line 2: expected 2 fields"),
        ("I,Q\n0", {}, "line 2: expected 2 fields"),
        ("A,phi\n1,inf", {}, "line 2: sample values must be finite"),
        ("I,Q\n0,nan", {}, "line 2: sample values must be finite"),
        ("A,phi\n1.01,0", {}, "line 2: sample magnitude"),
        ("I,Q\n0,0\n\n0.8,0.7", {}, "line 4: sample magnitude"),
        ('I,Q\n0,"0', {}, "line 2:"),
    ],
)
def test_invalid_file_or_ambiguous_options(tmp_path, body, options, error):
    path = tmp_path / "invalid.csv"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        load_iq_csv(path, **options)
