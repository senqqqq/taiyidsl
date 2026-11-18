"""Target-independent event containers; scheduled times are target ticks."""

from dataclasses import dataclass, field


class CompileError(ValueError):
    pass


@dataclass
class ScheduledEvent:
    kind: str
    signal: str
    start_mu: int
    duration_mu: int
    params: dict = field(default_factory=dict)
    source: str = ""
    resources: tuple = ()
    logical_ids: tuple = ()

    @property
    def end_mu(self):
        return self.start_mu + self.duration_mu


@dataclass
class CompiledPoint:
    index: int
    coordinates: dict
    axis_indices: tuple
    logical_events: list[ScheduledEvent]
    physical_events: list[ScheduledEvent]
    duration_mu: int
    origin_mu: int


@dataclass
class PhysicalPlan:
    """Backend output for a shot, with pre-roll before the logical origin."""

    events: list[ScheduledEvent]
    origin_mu: int


def acquisition_targets(event):
    """A shared detector acquisition can observe several qubits at once."""
    if event.kind == "measure":
        return (event.params["qubit"],)
    if event.kind == "acquire":
        return tuple(event.params["qubits"])
    return ()
