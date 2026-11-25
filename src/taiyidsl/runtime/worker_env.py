"""Inspect a separate ARTIQ worker without connecting to any instrument."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from taiyidsl import __version__

PROTOCOL = 1
LAUNCHER_ENV = "TAIYIDSL_ARTIQ_PYTHON"


def identity():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*.py"), key=lambda p: p.relative_to(root).as_posix()
    ):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(
            hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).digest()
        )
    return {
        "protocol": PROTOCOL,
        "taiyidsl": __version__,
        "source_sha256": digest.hexdigest(),
    }


def launch(explicit, *, spectrum, mode):
    configured = explicit or os.environ.get(LAUNCHER_ENV)
    if not configured and os.name == "nt":
        configured = Path.cwd() / ".toolchains/artiq9/clang64/bin/python.exe"
    if not configured:
        raise FileNotFoundError(
            f"Set LabConfig.artiq_python or {LAUNCHER_ENV} to the worker launcher"
        )
    # Calling a venv through its resolved symlink would select the system Python.
    python = Path(configured).expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise FileNotFoundError(
            f"ARTIQ Python not executable: {python}; "
            f"check LabConfig.artiq_python or {LAUNCHER_ENV}"
        )
    env = os.environ.copy()
    # spcm imports pyplot. A hardware worker has no notebook display and must
    # not try to load the authoring kernel's matplotlib_inline or GUI backend.
    env["MPLBACKEND"] = "Agg"
    for key in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    if os.name == "nt":
        # Legacy MSYS2 deployment uses the checkout, never the host's site-packages.
        src = Path(__file__).resolve().parents[2]
        if src.name == "src" and (src.parent / "pyproject.toml").is_file():
            env["PYTHONPATH"] = str(src)
        msys = python.parent.parent.parent / "usr/bin"
        if msys.is_dir():
            env["PATH"] = str(msys) + os.pathsep + env.get("PATH", "")
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [str(python), "-P", "-u", "-m", "taiyidsl.runtime.worker_entry", mode]
    return cmd + (["--spectrum"] if spectrum else []), env


def inspect_worker(*, spectrum, load_driver=False):
    """Metadata check. Optional library loading resolves symbols, never calls them."""
    modules = ["artiq", "llvmlite"] + (["spcm", "spcm_core"] if spectrum else [])
    packages = {}
    for name in modules:
        spec = importlib.util.find_spec(name)
        if spec is None:
            raise RuntimeError(f"Worker missing {name!r} in {sys.executable}")
        packages[name] = {
            "path": spec.origin,
            "version": importlib.metadata.version(name),
        }
    if packages["artiq"]["version"].split(".")[0] != "9":
        raise RuntimeError(f"ARTIQ 9 required, found {packages['artiq']['version']}")
    tools = {name: shutil.which(name) for name in ("ld.lld", "llvm-strip")}
    if not all(tools.values()):
        raise RuntimeError(f"Missing ARTIQ compiler tool(s): {tools}")
    if load_driver:
        name = "spcm_win64.dll" if os.name == "nt" else "libspcm_linux.so"
        try:
            lib = (ctypes.WinDLL if os.name == "nt" else ctypes.CDLL)(name)
            getattr(lib, "spcm_hOpen")
        except (OSError, AttributeError) as exc:
            raise RuntimeError(f"Cannot load Spectrum driver {name}: {exc}") from exc
    return {
        **identity(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "package_path": str(Path(__file__).resolve().parents[1]),
        "packages": packages,
        "tools": tools,
        "driver_library_loaded": load_driver,
    }


def verify(report):
    for key, expected in identity().items():
        if report.get(key) != expected:
            raise RuntimeError(
                f"Worker {key} mismatch: host={expected}, worker={report.get(key)}. "
                "Rebuild/reselect the worker; no connect request sent."
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", help=f"Worker launcher (default: {LAUNCHER_ENV})")
    parser.add_argument(
        "--artiq-only", action="store_true", help="Do not require Spectrum"
    )
    parser.add_argument(
        "--load-driver",
        action="store_true",
        help="Additionally load the native Spectrum library, without opening a card",
    )
    args = parser.parse_args()
    if args.artiq_only and args.load_driver:
        parser.error("--load-driver requires the hybrid worker")
    cmd, env = launch(
        args.python,
        spectrum=not args.artiq_only,
        mode="inspect-driver" if args.load_driver else "inspect",
    )
    completed = subprocess.run(
        cmd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    if completed.returncode:
        parser.exit(1, f"Worker {cmd[0]} failed: {completed.stderr}\n")
    response = json.loads(completed.stdout)
    if not response["ok"]:
        parser.exit(1, response["error"] + "\n")
    verify(response["result"])
    print(json.dumps(response["result"], indent=2))


if __name__ == "__main__":
    main()
