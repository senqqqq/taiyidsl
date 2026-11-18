"""Point selection changes compiler coverage, never the exported source/manifest."""

import runpy
from pathlib import Path

import pytest


def test_compile_filter_retains_initialize_finalize_and_requested_parameter_rows():
    functions = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "examples"
            / "artiq_offline_compile.py"
        )
    )
    select = functions["select_kernel_jobs"]
    jobs = [
        ("initialize_devices", None, []),
        ("body_0", 0, [10]),
        ("body_0", 3, [20]),
        ("point_6", 6, []),
        ("finalize_native_session", None, []),
    ]
    assert select(jobs) is jobs
    assert select(jobs, [3, 6]) == [jobs[0], jobs[2], jobs[3], jobs[4]]
    for invalid in ([], [-1], [True], [1, True], [1.5], [9]):
        with pytest.raises(ValueError):
            select(jobs, invalid)
