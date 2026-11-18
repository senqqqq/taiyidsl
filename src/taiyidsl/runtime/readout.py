"""Structural typing for the existing host-side result adapter contracts.

These protocols neither load a detector SDK nor add runtime validation. Implement
the methods appropriate to the selected runner; inheritance is not required.
An acquisition or a native camera trigger alone does not retrieve an image.
"""

from typing import Protocol

from .contracts import ObservationCommit, ShotContext
from .run_store import RunStore

__all__ = ["ResultAdapter", "PersistentResultAdapter", "RecoverableResultAdapter"]


class ResultAdapter(Protocol):
    """Adapter assigned to an ordinary exported ARTIQ host program.

    The host calls before_shot, executes the shot, then calls after_shot. It
    ignores both return values and calls finish after successful completion of
    the loop. finish is not an exception-cleanup hook. The adapter owns storage.
    point and shot use the exported program's indices; a chunk-local export is
    not the global-index contract of StreamingRunner.
    """

    def before_shot(self, point: int, shot: int) -> object:
        """Arm the detector before the shot; do not emit the experiment trigger."""
        ...

    def after_shot(self, point: int, shot: int) -> object:
        """Read and store detector data after the shot returns."""
        ...

    def finish(self) -> object:
        """Finalize results after normal loop completion."""
        ...


class PersistentResultAdapter(Protocol):
    """Recommended contextual after_shot contract for StreamingRunner.

    point and shot are global indices; context identifies the exact attempt.
    Return JSON-compatible observations or a receipt from
    run_store.commit_observation(context, values, assets=...). Save raw assets
    before committing; never return NumPy frames directly in a JSON mapping.

    before_shot(point, shot, *, context, run_store) and finish() are optional
    hooks, intentionally absent from this protocol's required members. Existing
    two-argument callbacks remain supported: the runner passes context and
    run_store only when those parameter names occur in the method signature.
    This protocol describes the recommended contextual shape, not every legacy
    shape accepted by the runner. finish runs only after normal completion.
    """

    def after_shot(
        self, point: int, shot: int, *, context: ShotContext, run_store: RunStore
    ) -> dict[str, object] | ObservationCommit:
        """Return durable observations for this hardware-completed attempt."""
        ...


class RecoverableResultAdapter(Protocol):
    """Optional host-only recovery capability, used with a persistent adapter.

    Called for an earlier hardware_complete attempt with no observation receipt.
    Find already saved data for that exact context, without arming, triggering,
    or executing hardware. Return None if unavailable; the runner's default
    unresolved policy then stops. Unknown hardware outcomes are not recovered
    by this hook. Recovery must not manufacture a missing-frame measurement.
    """

    def recover_shot(
        self, point: int, shot: int, *, context: ShotContext, run_store: RunStore
    ) -> dict[str, object] | ObservationCommit | None:
        """Recover the original attempt's data, never create a new attempt."""
        ...
