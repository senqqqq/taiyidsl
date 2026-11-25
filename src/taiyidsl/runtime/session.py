"""Explicit hardware sessions hosted by the separate ARTIQ Python process."""

from __future__ import annotations

import copy
import json
import queue
import subprocess
import threading
import uuid
from pathlib import Path

from . import worker_env
from ._cleanup import run_cleanup


def _stop_process(process, *, force):
    """Finish or kill the worker before closing its pipes."""
    try:
        if process.poll() is not None:
            return
        if not force:
            try:
                process.stdin.write('{"command":"close"}\n')
                process.stdin.flush()
                process.wait(timeout=10)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=10)
    except BaseException as error:
        run_cleanup(
            [
                ("kill ARTIQ worker", process.kill),
                ("wait for ARTIQ worker", lambda: process.wait(timeout=10)),
            ],
            primary_error=error,
        )
        raise


class HardwareSession:
    """Own connections and execution receipts; never synthesize detector data.

    Constructed through LabConfig.connect(), after the emulation guard.  Native
    dependencies and spcm must be installed in the selected ARTIQ environment.
    A connected session is exclusive: it must not share the core or cards with
    another experiment runner.
    """

    def __init__(self, lab, *, clock_session_ids=None):
        if lab.emulation:
            raise RuntimeError("HardwareSession is unavailable in emulation mode")
        if not hasattr(lab.setup, "_device_db"):
            raise ValueError("Hardware sessions require a database-backed DeviceSetup")
        self.lab = copy.copy(lab)
        self.lab.setup = copy.deepcopy(lab.setup)
        self.lab.signal_map = copy.deepcopy(lab.signal_map)
        # Qubit values contain immutable MappingProxyType fields; use their
        # explicit serialization snapshot rather than pickle-based deepcopy.
        self.lab.qubits = tuple(qubit.snapshot() for qubit in lab.qubits)
        self.clock_session_ids = dict(clock_session_ids or {})
        cards = set(getattr(lab.setup, "_spectrum_addresses", {}))
        if set(self.clock_session_ids) - cards or any(
            not isinstance(value, str) or not value
            for value in self.clock_session_ids.values()
        ):
            raise ValueError("clock_session_ids require known cards and nonempty IDs")
        self._process = None
        self._messages = queue.Queue()
        self._stderr = None
        self._reader = None
        self.directory = None
        self.observed = None

    def _receive(self, timeout):
        try:
            try:
                line = self._messages.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(
                    "ARTIQ worker did not respond; execution state is unknown"
                ) from exc
            if not line:
                raise RuntimeError(
                    f"ARTIQ worker exited; execution state is unknown. "
                    f"See {self.directory / 'worker.log'}"
                )
            response = json.loads(line)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "Hardware worker failed"))
            return response["result"]
        except BaseException as error:
            run_cleanup(
                [("failed hardware session", lambda: self.close(force=True))],
                primary_error=error,
            )
            raise

    def open(self):
        if self._process is not None:
            return self
        root = Path.cwd()
        command, environment = worker_env.launch(
            self.lab.artiq_python,
            spectrum=self.lab.setup.spectrum is not None,
            mode="serve",
        )
        self.directory = root / "output/hardware_sessions" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        setup = self.lab.setup
        request = {
            "device_db": setup._device_db,
            "spectrum": setup.spectrum.to_dict()
            if setup.spectrum is not None
            else None,
            "addresses": setup._spectrum_addresses,
            "clock_session_ids": self.clock_session_ids,
        }
        config = self.directory / "connection.json"
        config.write_text(json.dumps(request, indent=2), encoding="utf-8")
        self._stderr = (self.directory / "worker.log").open("w", encoding="utf-8")
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                encoding="utf-8",
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            process = self._process
            messages = self._messages = queue.Queue()

            def receive_lines():
                try:
                    for line in process.stdout:
                        messages.put(line)
                except (OSError, ValueError):
                    # Closing a failed worker can also close a pending read.
                    pass
                finally:
                    messages.put("")

            self._reader = threading.Thread(target=receive_lines, daemon=True)
            self._reader.start()
            report = self._receive(60)
            worker_env.verify(report)
            (self.directory / "worker_environment.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            process.stdin.write(
                json.dumps({"command": "connect", "config": str(config)}) + "\n"
            )
            process.stdin.flush()
            self.observed = self._receive(60)
        except BaseException as error:
            run_cleanup(
                [("failed hardware session", lambda: self.close(force=True))],
                primary_error=error,
            )
            raise
        return self

    def run(self, compiled):
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError("Session is not connected")
        compiled.assert_integrity()
        authoring = compiled.metadata.get("device_setup", {})
        if (
            authoring.get("descriptor") != self.lab.setup._descriptor
            or authoring.get("databases") != self.lab.setup._database_sources
        ):
            raise ValueError("Compiled setup differs from the connected configuration")
        if any(compiled.acquisition_plan(p.index) for p in compiled.points):
            raise ValueError(
                "This session has no detector adapter; "
                "acquisition data cannot be invented"
            )
        export = self.directory / f"run_{uuid.uuid4().hex}"
        compiled.export(export)
        try:
            self._process.stdin.write(
                json.dumps({"command": "run", "directory": str(export)}) + "\n"
            )
            self._process.stdin.flush()
            # Long experiments intentionally have no arbitrary timeout.
            result = self._receive(None)
        except BaseException as error:
            run_cleanup(
                [("failed hardware session", lambda: self.close(force=True))],
                primary_error=error,
            )
            raise
        (export / "execution_receipt.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        return result

    def close(self, *, force=False):
        process, self._process = self._process, None
        stderr, self._stderr = self._stderr, None
        reader, self._reader = self._reader, None
        actions = []
        if process is not None:
            actions.append(
                ("stop ARTIQ worker", lambda: _stop_process(process, force=force))
            )
            for name, stream in (("stdin", process.stdin), ("stdout", process.stdout)):
                if stream is not None:
                    actions.append((f"close worker {name}", stream.close))
        if reader is not None:
            actions.append(("join worker reader", lambda: reader.join(timeout=1)))
        if stderr is not None:
            actions.append(("close worker log", stderr.close))
        run_cleanup(actions)

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        run_cleanup([("hardware session", self.close)], primary_error=exc)
