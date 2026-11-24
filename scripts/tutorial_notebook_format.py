"""Write a tutorial with the same notebook formatting used by repository CI."""

import subprocess
import sys

import nbformat


def write_formatted_notebook(notebook, path):
    nbformat.validate(notebook)
    nbformat.write(notebook, path)
    ruff = [sys.executable, "-m", "ruff"]
    subprocess.run([*ruff, "check", "--select", "I", "--fix", path], check=True)
    subprocess.run([*ruff, "format", path], check=True)
    subprocess.run([*ruff, "check", path], check=True)
