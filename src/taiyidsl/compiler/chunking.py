"""Compile host-axis chunks and render standalone host iteration loops."""

from __future__ import annotations

import math

from ..ir import CompileError


def build_execution_plan(parameters, sweep_nodes, average):
    """Partition one host axis without changing global point coordinates."""
    scope = average.params.get("scope", "global")
    if scope not in {"global", "chunk"}:
        raise CompileError("Average scope must be global or chunk")
    selected = [
        (index, node)
        for index, node in enumerate(sweep_nodes)
        if node.params.get("chunks") is not None
        or node.params.get("chunk_size") is not None
    ]
    if len(selected) > 1:
        raise CompileError("Only one host sweep axis may declare chunking")
    if scope == "chunk" and not selected:
        raise CompileError("Chunk averaging requires a chunked sweep axis")
    count = math.prod(len(parameter.values) for parameter in parameters)
    chunks = []
    axis = None
    if selected:
        index, node = selected[0]
        axis = parameters[index].name
        length = len(parameters[index].values)
        number, size = node.params.get("chunks"), node.params.get("chunk_size")
        if number is not None and size is not None:
            raise CompileError("chunks and chunk_size are mutually exclusive")
        value = number if number is not None else size
        if number == "auto":
            sizes = [length]
        elif type(value) is not int or value < 1:
            raise CompileError("chunks and chunk_size require positive integers")
        elif number is not None:
            if number > length:
                raise CompileError("chunks cannot exceed the number of axis values")
            quotient, remainder = divmod(length, number)
            sizes = [quotient + (i < remainder) for i in range(number)]
        else:
            sizes = [min(size, length - start) for start in range(0, length, size)]
        outer = math.prod(len(p.values) for p in parameters[:index])
        inner = math.prod(len(p.values) for p in parameters[index + 1 :])
        for prefix in range(outer):
            begin = prefix * length * inner
            for width in sizes:
                end = begin + width * inner
                chunks.append({"index": len(chunks), "points": list(range(begin, end))})
                begin = end
    else:
        # A point is the historical upload unit; repeated shots can reuse it.
        chunks = [{"index": i, "points": [i]} for i in range(count)]
    plan = {
        "schema_version": 1,
        "shots": average.params["shots"],
        "averaging_mode": average.params["mode"],
        "averaging_scope": scope,
        "chunk_axis": axis,
        "point_count": count,
        "chunks": chunks,
    }
    if selected and selected[0][1].params.get("chunks") == "auto":
        plan.update(chunk_policy="auto", atomic_point_count=inner)
    return plan


def resolve_auto_chunks(points, plan):
    """Resolve consecutive axis-value groups against all physical card limits."""
    from taiyidsl.backends.artiq_spectrum.banks import BankBudget

    if plan.get("chunk_policy") != "auto":
        return plan
    chunks = []
    width = plan["atomic_point_count"]
    for outer in plan["chunks"]:
        current, budget = [], BankBudget()
        indices = outer["points"]
        for begin in range(0, len(indices), width):
            unit = indices[begin : begin + width]
            addition = BankBudget()
            for index in unit:
                addition.add_point(points[index])
            try:
                addition.validate()
            except CompileError as exc:
                raise CompileError(
                    "One value of the auto chunk axis and its inner combinations "
                    "exceeds capacity; select an inner axis. A single shot cannot "
                    f"be split: {exc}"
                ) from exc
            candidate = budget.merged(addition)
            try:
                candidate.validate()
            except CompileError:
                chunks.append({"index": len(chunks), "points": current})
                current, budget = list(unit), addition
            else:
                current.extend(unit)
                budget = candidate
        if current:
            chunks.append({"index": len(chunks), "points": current})
    return {**plan, "chunks": chunks}


def host_loop_lines(plan, *, indent="        "):
    """Emit standalone Python loops directly from the shared compact plan."""
    lines = []
    if plan["averaging_scope"] == "chunk":
        batches = tuple(tuple(c["points"]) for c in plan["chunks"])
        lines.append(f"{indent}for points in {batches!r}:")
        indent += "    "
        points = "points"
    else:
        points = f"range({plan['point_count']})"
    if plan["averaging_mode"] == "cyclic":
        lines += [
            f"{indent}for shot in range({plan['shots']}):",
            f"{indent}    for point in {points}:",
        ]
    else:
        lines += [
            f"{indent}for point in {points}:",
            f"{indent}    for shot in range({plan['shots']}):",
        ]
    return [*lines, f"{indent}        self._run_shot(point, shot)"]
