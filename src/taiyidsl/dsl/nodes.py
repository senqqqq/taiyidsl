"""The single unscheduled node type recorded by all DSL builders."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    kind: str
    params: dict = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    source: str = ""
