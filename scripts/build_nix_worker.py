"""Build the locked Nix worker from package sources, never from lab data.

Useful in a dirty/in-progress checkout: a git flake ignores new source files,
while an unfiltered path flake can copy databases or ignored data to the store.
"""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def stage(root, dest):
    for name in ("flake.nix", "flake.lock", "pyproject.toml", "uv.lock", "README.md"):
        shutil.copyfile(root / name, dest / name)
    package = root / "src/taiyidsl"
    # Include only Python modules and the wheel's shipped viewer resources.
    for path in package.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Refusing linked package source: {path}")
        if path.suffix != ".py" and not (
            path.parent == package / "reporting/assets"
            and path.suffix in {".js", ".html"}
        ):
            continue
        target = dest / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-link", type=Path, default=root / "local_config/hardware-worker"
    )
    args = parser.parse_args()
    link = args.out_link.absolute()
    link.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="taiyidsl-nix-") as tmp:
        stage(root, Path(tmp))
        subprocess.run(
            [
                "nix",
                "build",
                "--no-update-lock-file",
                "--out-link",
                str(link),
                f"path:{tmp}#hardware-worker",
            ],
            check=True,
        )
    print(link / "bin/taiyidsl-artiq-python")


if __name__ == "__main__":
    main()
