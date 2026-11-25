"""Fail closed before handing a worker any usable instrument configuration."""

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from taiyidsl.runtime import worker_entry, worker_env


def test_worker_does_not_inherit_notebook_plot_backend(monkeypatch):
    inline = "module://matplotlib_inline.backend_inline"
    monkeypatch.setenv("MPLBACKEND", inline)
    _, child = worker_env.launch(sys.executable, spectrum=True, mode="serve")
    assert child["MPLBACKEND"].lower() == "agg"
    assert os.environ["MPLBACKEND"] == inline


def test_explicit_launcher_wins_and_keeps_venv_entry(tmp_path, monkeypatch):
    explicit = sys.executable
    monkeypatch.setenv(worker_env.LAUNCHER_ENV, str(tmp_path / "missing"))
    monkeypatch.setenv("PYTHONPATH", "foreign/site-packages")
    cmd, env = worker_env.launch(explicit, spectrum=False, mode="inspect")
    assert cmd[0] == str(Path(explicit).absolute())
    assert "foreign/site-packages" not in env.get("PYTHONPATH", "")
    assert "--spectrum" not in cmd
    with pytest.raises(FileNotFoundError, match="missing"):
        worker_env.launch(None, spectrum=False, mode="serve")
    monkeypatch.delenv(worker_env.LAUNCHER_ENV)
    if os.name != "nt":
        with pytest.raises(FileNotFoundError, match=worker_env.LAUNCHER_ENV):
            worker_env.launch(None, spectrum=False, mode="serve")


def test_doctor_separates_module_discovery_and_driver_load(monkeypatch):
    found = []

    def spec(name):
        found.append(name)
        return SimpleNamespace(origin=name)

    monkeypatch.setattr(worker_env.importlib.util, "find_spec", spec)
    monkeypatch.setattr(worker_env.importlib.metadata, "version", lambda name: "9.1")
    monkeypatch.setattr(worker_env.shutil, "which", lambda name: name)

    def missing(path):
        raise OSError("test library missing")

    monkeypatch.setattr(
        worker_env.ctypes, "WinDLL" if os.name == "nt" else "CDLL", missing
    )
    assert not worker_env.inspect_worker(spectrum=False)["driver_library_loaded"]
    assert found == ["artiq", "llvmlite"]
    assert set(worker_env.inspect_worker(spectrum=True)["packages"]) >= {
        "spcm",
        "spcm_core",
    }
    with pytest.raises(RuntimeError, match="test library missing"):
        worker_env.inspect_worker(spectrum=True, load_driver=True)


def test_missing_module_never_reaches_connect(monkeypatch):
    monkeypatch.setattr(worker_env.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(
        worker_entry, "Worker", lambda *_: pytest.fail("constructed hardware worker")
    )
    incoming = io.StringIO('{"command":"connect", "config":"must-not-read"}\n')
    result = io.StringIO()
    worker_entry.serve("serve", True, incoming, result)
    assert incoming.tell() == 0
    assert "Worker missing 'artiq'" in json.loads(result.getvalue())["error"]
