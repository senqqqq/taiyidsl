"""Named CSV fields preserve the source records used to select a pulse."""

import numpy as np
import pytest

from taiyidsl import load_waveform_csv


def test_crop_by_scheme_and_time_preserves_both_sides_of_phase_step(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text(
        "scheme,time_us,rabi_khz,phase_rad,inphase_khz,quadrature_khz\n"
        "mpp,0,80,0,80,0\n"
        '"other, sweep",1.75,60,0,60,0\n'
        "mpp,1.75,80,0,80,0\n"
        "mpp,1.75,80,3.141592653589793,-80,9.8e-15\n"
        "mpp,3,0,0,0,0\n",
        encoding="utf-8-sig",
    )
    records = load_waveform_csv(path)
    assert isinstance(records, np.ndarray)
    assert records.shape == (5,)
    assert records.dtype.names == (
        "scheme",
        "time_us",
        "rabi_khz",
        "phase_rad",
        "inphase_khz",
        "quadrature_khz",
    )
    assert records["rabi_khz"].dtype.kind in "iu"
    assert records["time_us"].dtype.kind == "f"
    assert records["scheme"].tolist() == ["mpp", "other, sweep", "mpp", "mpp", "mpp"]
    mpp = records[records["scheme"] == "mpp"]
    crop = mpp[(mpp["time_us"] >= 1.5) & (mpp["time_us"] <= 2)]
    np.testing.assert_array_equal(crop["time_us"], [1.75, 1.75])
    np.testing.assert_array_equal(crop["rabi_khz"], [80, 80])
    np.testing.assert_array_equal(crop["phase_rad"], [0, np.pi])
    np.testing.assert_array_equal(crop["inphase_khz"], [80, -80])
    np.testing.assert_array_equal(crop["quadrature_khz"], [0, 9.8e-15])


def test_headerless_input_uses_generated_names_for_each_column(tmp_path):
    path = tmp_path / "without_header.csv"
    path.write_text('1;2.5;"café; soir"\n-3;0;01\n', encoding="cp1252")
    records = load_waveform_csv(path, header=False, delimiter=";", encoding="cp1252")
    assert records.dtype.names == ("col0", "col1", "col2")
    np.testing.assert_array_equal(records["col0"], [1, -3])
    np.testing.assert_array_equal(records["col1"], [2.5, 0])
    assert records["col2"].tolist() == ["café; soir", "01"]


def test_complex_columns_accept_i_j_and_real_entries(tmp_path):
    path = tmp_path / "iq.csv"
    path.write_text("channel,IQ\nchA,3-2i\nchB,-1e2+2.5j\nchC,4\n", encoding="utf-8")
    records = load_waveform_csv(path)
    assert records["IQ"].dtype.kind == "c"
    np.testing.assert_array_equal(records["IQ"], [3 - 2j, -100 + 2.5j, 4 + 0j])


def test_numeric_looking_labels_with_unmatched_parentheses_stay_text(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("label\n(2i\n3)\n", encoding="utf-8")
    assert load_waveform_csv(path)["label"].tolist() == ["(2i", "3)"]


def test_numeric_blanks_become_nan_and_label_text_survives_inference(tmp_path):
    path = tmp_path / "missing.csv"
    path.write_text(
        "amplitude,empty,label,serial\n12,,007,0008\n,,x,0730\n-3.5,,,0000\n",
        encoding="utf-8",
    )
    records = load_waveform_csv(path, text_columns=("serial",))
    assert records["amplitude"].dtype.kind == "f"
    np.testing.assert_allclose(records["amplitude"], [12, np.nan, -3.5], equal_nan=True)
    assert records["empty"].tolist() == ["", "", ""]
    assert records["label"].tolist() == ["007", "x", ""]
    assert records["serial"].tolist() == ["0008", "0730", "0000"]


def test_header_only_input_keeps_fields_with_zero_records(tmp_path):
    path = tmp_path / "header_only.csv"
    path.write_text("Time (us),Measured A\n", encoding="utf-8")
    records = load_waveform_csv(path)
    assert records.shape == (0,)
    assert records.dtype.names == ("Time (us)", "Measured A")


@pytest.mark.parametrize(
    ("body", "options"),
    [
        ("a,b\n1,2,3\n", {}),
        ("a,b\n1\n", {}),
        ("x,x\n1,2\n", {}),
        ("x,y\n1,2\n", {"text_columns": ("missing",)}),
    ],
)
def test_malformed_records_or_unknown_columns_rejected(tmp_path, body, options):
    path = tmp_path / "invalid.csv"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_waveform_csv(path, **options)
