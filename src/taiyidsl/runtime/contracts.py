"""Shared host execution values, without storage or instrument dependencies."""

from dataclasses import dataclass
from typing import Literal


class RecoveryRequired(RuntimeError):
    """A shot may have run; an explicit retry/skip decision is required."""


@dataclass(frozen=True)
class ShotContext:
    run_id: str
    execution_index: int
    point: int
    shot: int
    attempt: int
    session_id: str


@dataclass(frozen=True)
class ObservationCommit:
    path: str
    sha256: str
    context: ShotContext


AttemptStatus = Literal[
    "started",
    "prepared",
    "kernel_entered",
    "hardware_complete",
    "data_committed",
    "failed_before_hardware",
    "unknown",
    "retry_authorized",
    "skipped",
]
RecoveryPolicy = Literal["error", "retry", "skip"]
RecoveryDecision = Literal["retry", "skip"]
