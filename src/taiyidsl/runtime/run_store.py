"""Durable shot attempts and observation commits; never infer a physical retry."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .._artifact_io import atomic_json, file_sha256
from .contracts import (
    AttemptStatus,
    ObservationCommit,
    RecoveryDecision,
    RecoveryPolicy,
    RecoveryRequired,
    ShotContext,
)


class RunStore:
    """SQLite journal plus immutable per-attempt observations on local disk.

    Hardware completion and committed detector data are separate states. SQLite
    transactions use FULL synchronous durability; observation files are fsynced
    then atomically renamed before their commit is recorded in the journal.
    """

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory / "run.sqlite3")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                execution_index INTEGER, attempt INTEGER, context TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT,
                PRIMARY KEY(execution_index, attempt));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, time REAL NOT NULL, execution_index INTEGER,
                attempt INTEGER, stage TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        version = self._metadata("schema_version")
        if version not in {None, "1"}:
            self.connection.close()
            raise ValueError("Unsupported RunStore schema version")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')"
            )
        self.run_id = self._metadata("run_id")
        if self.run_id is None:
            self.run_id = uuid.uuid4().hex
            with self.connection:
                self.connection.execute(
                    "INSERT INTO metadata VALUES (?, ?)", ("run_id", self.run_id)
                )

    def _metadata(self, key):
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    @contextmanager
    def exclusive(self):
        """One executor owns this run; OS locks disappear after a process crash."""
        stream = (self.directory / "executor.lock").open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("Another executor already owns this RunStore") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def start_session(self, artifact_sha256):
        previous = self._metadata("artifact_sha256")
        if previous is not None and previous != artifact_sha256:
            raise ValueError("RunStore belongs to a different compiled artifact")
        session = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES (?, ?)",
                ("artifact_sha256", artifact_sha256),
            )
            self.connection.execute(
                "INSERT INTO events(time, stage, payload) VALUES (?, ?, ?)",
                (time.time(), "session_started", json.dumps({"session_id": session})),
            )
        return session

    def latest(self, execution_index):
        row = self.connection.execute(
            "SELECT context,status,receipt FROM attempts WHERE execution_index=? "
            "ORDER BY attempt DESC LIMIT 1",
            (execution_index,),
        ).fetchone()
        if row is None:
            return None
        return {
            "context": ShotContext(**json.loads(row[0])),
            "status": row[1],
            "receipt": json.loads(row[2]) if row[2] else None,
        }

    def finish_session(self, session_id, *, error=None):
        """Record lifecycle/initialization failures even before the first shot."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO events(time, stage, payload) VALUES (?, ?, ?)",
                (
                    time.time(),
                    "session_finished",
                    json.dumps({"session_id": session_id, "error": error}),
                ),
            )

    def begin(self, execution_index, point, shot, session_id) -> ShotContext:
        previous = self.latest(execution_index)
        attempt = 0 if previous is None else previous["context"].attempt + 1
        context = ShotContext(
            self.run_id, execution_index, point, shot, attempt, session_id
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, NULL)",
                (execution_index, attempt, json.dumps(asdict(context)), "started"),
            )
        self.record(context, "started")
        return context

    def record(
        self,
        context: ShotContext,
        stage,
        payload=None,
        *,
        status: AttemptStatus | None = None,
    ):
        encoded = json.dumps(payload or {}, allow_nan=False)
        with self.connection:
            self.connection.execute(
                "INSERT INTO events(time,execution_index,attempt,stage,payload) "
                "VALUES (?,?,?,?,?)",
                (time.time(), context.execution_index, context.attempt, stage, encoded),
            )
            if status is not None:
                self.connection.execute(
                    "UPDATE attempts SET status=?,receipt=? "
                    "WHERE execution_index=? AND attempt=?",
                    (status, encoded, context.execution_index, context.attempt),
                )

    def _observation_path(self, context):
        if context.run_id != self.run_id:
            raise ValueError("Observation belongs to another run")
        return (
            self.directory
            / "observations"
            / f"{context.execution_index}_{context.attempt}.json"
        )

    def commit_observation(
        self, context: ShotContext, values, *, assets=()
    ) -> ObservationCommit:
        """Persist JSON observations and references to already saved raw data.

        Raw images/oscilloscope arrays remain separate files; every referenced
        file is hashed. Detector adapters return this receipt from after_shot.
        """
        files = []
        for value in assets:
            path = Path(value).resolve()
            if not path.is_file():
                raise ValueError(f"Observation asset does not exist: {path}")
            files.append({"path": str(path), "sha256": file_sha256(path)})
        path = self._observation_path(context)
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            raise FileExistsError(
                "An attempt's observation is immutable; use a new attempt"
            )
        atomic_json(
            path, {"context": asdict(context), "values": values, "assets": files}
        )
        return ObservationCommit(str(path), file_sha256(path), context)

    def recover_observation(self, context: ShotContext) -> ObservationCommit | None:
        path = self._observation_path(context)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("context") != asdict(context):
            raise ValueError("Observation context differs from its journal attempt")
        for asset in value.get("assets", []):
            if file_sha256(asset["path"]) != asset["sha256"]:
                raise ValueError("Observation asset changed after commit")
        return ObservationCommit(str(path), file_sha256(path), context)

    def commit(self, context: ShotContext, receipt: ObservationCommit):
        if not isinstance(receipt, ObservationCommit) or receipt.context != context:
            raise TypeError(
                "after_shot must return an ObservationCommit for this attempt"
            )
        observed = self.recover_observation(context)
        if observed != receipt:
            raise ValueError("Observation commit does not match durable data")
        self.record(context, "data_committed", asdict(receipt), status="data_committed")

    def resolve(self, execution_index, decision: RecoveryDecision, *, reason):
        """Explicitly authorize a new attempt, or mark an unresolved shot missing."""
        if decision not in {"retry", "skip"} or not reason:
            raise ValueError("Choose retry or skip and provide a reason")
        previous = self.latest(execution_index)
        if previous is None or previous["status"] == "data_committed":
            raise ValueError("There is no unresolved attempt to resolve")
        self.record(
            previous["context"],
            "recovery_decision",
            {"decision": decision, "reason": reason},
            status="retry_authorized" if decision == "retry" else "skipped",
        )

    def should_execute(self, execution_index, *, unresolved: RecoveryPolicy = "error"):
        if unresolved not in {"error", "retry", "skip"}:
            raise ValueError("unresolved must be error, retry or skip")
        previous = self.latest(execution_index)
        if previous is None:
            return True
        status, context = previous["status"], previous["context"]
        if status == "data_committed":
            recovered = self.recover_observation(context)
            if recovered is None or asdict(recovered) != previous["receipt"]:
                raise ValueError("Committed observation changed or is missing")
            return False
        if status == "skipped":
            return False
        if status == "hardware_complete":
            recovered = self.recover_observation(context)
            if recovered is not None:
                self.commit(context, recovered)
                return False
        if status in {"failed_before_hardware", "retry_authorized"}:
            return True
        if unresolved != "error":
            self.resolve(execution_index, unresolved, reason="explicit resume policy")
            return unresolved == "retry"
        raise RecoveryRequired(
            f"Execution {execution_index} has status {status}; inspect detector data "
            "and explicitly resolve retry or skip. "
            "A physical shot may already have run."
        )

    def events(self):
        return [
            {
                "time": row[0],
                "execution_index": row[1],
                "attempt": row[2],
                "stage": row[3],
                "payload": json.loads(row[4]),
            }
            for row in self.connection.execute(
                "SELECT time,execution_index,attempt,stage,payload "
                "FROM events ORDER BY id"
            )
        ]

    def close(self):
        self.connection.close()
