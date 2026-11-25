"""Versioned worker handshake. No device configuration is provided at startup."""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from ._cleanup import run_cleanup
from .worker import Worker
from .worker_env import inspect_worker


def serve(mode, spectrum, incoming, protocol):
    def reply(result=None, error=None):
        protocol.write(
            json.dumps({"ok": error is None, "result": result, "error": error}) + "\n"
        )
        protocol.flush()

    worker = None
    try:
        reply(inspect_worker(spectrum=spectrum, load_driver=mode == "inspect-driver"))
        if mode != "serve":
            return
        for line in incoming:
            command = json.loads(line)
            if command["command"] == "close":
                break
            if worker is None and command["command"] == "connect":
                path = Path(command["config"]).resolve(strict=True)
                config = json.loads(path.read_text(encoding="utf-8"))
                if bool(config.get("spectrum")) != spectrum:
                    raise ValueError("Spectrum configuration differs from handshake")
                if spectrum:
                    # Fail before opening the core. SDK import may log its version.
                    from spcm import Card  # noqa: F401
                    from spcm_core import spcm_hOpen  # noqa: F401
                worker = Worker(config, path.parent)
                reply(worker.connect())
            elif worker is not None and command["command"] == "run":
                reply(worker.run(command["directory"]))
            else:
                raise ValueError("Invalid hardware worker command or session state")
    except BaseException as error:
        if worker is not None:
            run_cleanup([("failed worker", worker.close)], primary_error=error)
        reply(error=traceback.format_exc())
    finally:
        if worker is not None:
            worker.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["inspect", "inspect-driver", "serve"])
    parser.add_argument("--spectrum", action="store_true")
    args = parser.parse_args()
    # C libraries may write directly to fd 1, bypassing a Python stdout redirect.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as protocol:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        serve(args.mode, args.spectrum, sys.stdin, protocol)


if __name__ == "__main__":
    main()
