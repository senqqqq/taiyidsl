"""Execute the teaching notebook in a fresh project-Python kernel, offline.

Writes an executed copy, a static HTML reading copy, and a verification receipt.
No kernel registration, global Jupyter config, or instrument execution is used.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import nbformat
from jupyter_client import KernelManager
from nbclient import NotebookClient
from nbconvert import HTMLExporter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/tutorials/hybrid_from_scratch.ipynb"
OUTPUT = ROOT / "output/tutorial_from_scratch"


def main(*, source=SOURCE, output=OUTPUT, semantics=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for name, directory in (
        ("JUPYTER_RUNTIME_DIR", output / ".jupyter/runtime"),
        ("JUPYTER_CONFIG_DIR", output / ".jupyter/config"),
        ("IPYTHONDIR", output / ".ipython"),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(directory)
    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    semantics_path = output / "tutorial_semantics.json"
    verification_cell = None
    if semantics:
        # Keep verification machinery out of the published lesson and run it in
        # the same fresh kernel, after every user-authored cell has succeeded.
        script = ROOT / "scripts/tutorial_semantics.py"
        verification_cell = nbformat.v4.new_code_cell(
            "import runpy as _tutorial_runpy\n"
            f"_tutorial_capture = _tutorial_runpy.run_path({str(script)!r})"
            "['capture']\n"
            f"_tutorial_capture(globals(), {str(semantics_path)!r}, "
            f"source={str(source)!r})",
            metadata={"tags": ["injected-verification"]},
            id="taiyidsl-semantics-verification",
        )
        notebook.cells.append(verification_cell)
    manager = KernelManager(kernel_name="python3")
    manager.kernel_spec.argv = [
        sys.executable,
        "-m",
        "ipykernel_launcher",
        "-f",
        "{connection_file}",
    ]
    destination = output / f"{source.stem}.executed.ipynb"
    started = time.monotonic()
    receipt_path = output / "notebook_verification.json"
    receipt_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")

    def progress(cell, cell_index, **kwargs):
        if cell.cell_type == "code":
            print(
                f"Executing cell {cell_index}: {cell.source.splitlines()[0][:90]}",
                flush=True,
            )

    client = NotebookClient(
        notebook,
        km=manager,
        timeout=600,
        allow_errors=False,
        resources={"metadata": {"path": str(ROOT)}},
        on_cell_start=progress,
    )
    try:
        client.execute()
    except Exception as error:
        receipt_path.write_text(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False),
            encoding="utf-8",
        )
        raise
    finally:
        try:
            if manager.has_kernel:
                manager.shutdown_kernel(now=True)
        finally:
            if verification_cell is not None:
                notebook.cells.remove(verification_cell)
                notebook.metadata["taiyidsl_verification"] = {
                    "injected_code_cells": 1,
                    "semantics_capture_executed": (
                        verification_cell.execution_count is not None
                    ),
                    "semantics_receipt": semantics_path.name,
                }
        # Preserve useful failure context, but only publish a passed receipt below.
        # Keep local documentation links valid in the output reading copies.
        for cell in notebook.cells:
            if cell.cell_type != "markdown":
                continue

            def relocate(match):
                target = match.group(1)
                if "://" in target or target.startswith("#"):
                    return match.group(0)
                relative = os.path.relpath(source.parent / target, output)
                return "](" + Path(relative).as_posix() + ")"

            cell.source = re.sub(r"\]\(([^\s)]+)\)", relocate, cell.source)
        nbformat.write(notebook, destination)
    nbformat.validate(notebook)
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    assert all(cell.execution_count is not None for cell in code_cells)
    assert not any(out.output_type == "error" for c in code_cells for out in c.outputs)
    body, _ = HTMLExporter(template_name="lab").from_notebook_node(notebook)
    (output / f"{source.stem}.html").write_text(body, encoding="utf-8")
    receipt = {
        "status": "passed",
        "python": sys.version,
        "executable": sys.executable,
        "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "executed_notebook": str(destination),
        "cells": len(notebook.cells),
        "executed_code_cells": len(code_cells),
        "elapsed_seconds": time.monotonic() - started,
        "fresh_kernel": True,
        "hardware_executed": False,
        "browser_render_verified": False,
        "verification_code_cells": int(semantics),
    }
    if semantics:
        captured = json.loads(semantics_path.read_text(encoding="utf-8"))
        receipt["semantics"] = {
            "path": str(semantics_path),
            "experiments": sorted(captured["experiments"]),
        }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--semantics", action="store_true")
    args = parser.parse_args()
    main(source=args.source, output=args.output, semantics=args.semantics)
