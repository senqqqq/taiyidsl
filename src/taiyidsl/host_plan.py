"""Read compact execution plans shared by artifacts, results, and viewers."""


def execution_plan_for(compiled):
    """Accept legacy compiled-like adapters that predate host chunk metadata."""
    plan = getattr(compiled, "host_execution_plan", None)
    if plan:
        return plan
    return {
        "schema_version": 1,
        "shots": compiled.shots,
        "averaging_mode": compiled.averaging_mode,
        "averaging_scope": "global",
        "chunk_axis": None,
        "point_count": len(compiled.points),
        "chunks": [{"index": p.index, "points": [p.index]} for p in compiled.points],
    }


def execution_order(plan):
    """Yield global (point, shot) keys, never expand their product in memory."""
    shots = plan["shots"]
    if plan["averaging_scope"] == "chunk":
        batches = (chunk["points"] for chunk in plan["chunks"])
    else:
        batches = (range(plan["point_count"]),)
    for points in batches:
        if plan["averaging_mode"] == "cyclic":
            for shot in range(shots):
                for point in points:
                    yield point, shot
        else:
            for point in points:
                for shot in range(shots):
                    yield point, shot


def point_execution_layout(plan):
    """Compact offsets used by viewers to calculate each shot's execution index."""
    result = {}
    preceding = 0
    for chunk in plan["chunks"]:
        for local, point in enumerate(chunk["points"]):
            result[point] = {
                "chunk": chunk["index"],
                "scope": plan["averaging_scope"],
                "offset": preceding * plan["shots"],
                "local_point": local,
                "point_count": len(chunk["points"]),
            }
        preceding += len(chunk["points"])
    return result
