"""Attempt every owned cleanup action without replacing an active failure."""

from collections.abc import Callable, Iterable


def run_cleanup(
    actions: Iterable[tuple[str, Callable[[], object]]],
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Run actions in order, retaining the first failure and later error notes.

    With ``primary_error``, callers remain responsible for propagating that
    exception. Without it, the first cleanup failure is raised after every
    action has been attempted. BaseException subclasses retain their identity.
    """
    first_error = primary_error
    for name, action in actions:
        try:
            action()
        except BaseException as error:
            if first_error is None:
                first_error = error
                first_error.add_note(f"Cleanup action failed: {name}")
            else:
                first_error.add_note(
                    f"Cleanup action {name!r} also failed: "
                    f"{type(error).__name__}: {error}"
                )
    if primary_error is None and first_error is not None:
        raise first_error
