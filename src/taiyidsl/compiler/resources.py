"""Derive scheduling conflicts through the scheduler resource interface."""

from ..ir import CompileError


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _frame(scheduler, signal):
    """Compatibility helper; associations belong to the scheduler."""
    return scheduler.signal_frame(signal)


def qubit_resources(scheduler, identifiers):
    """Include aliased outputs so a targeted barrier also joins their users."""
    resources = set()
    for uid in identifiers:
        if uid not in scheduler.exp.qubits:
            raise CompileError(f"Unknown barrier/layer qubit {uid}")
        resources.add(("qubit", uid))
        for signal in scheduler.mapping:
            frame = _frame(scheduler, signal)
            if (
                scheduler.signal_qubit(signal)[0] is not scheduler.exp.qubits[uid]
                and scheduler.frame_owner(frame) != uid
            ):
                continue
            resources.add(("signal", signal))
            resources.update(
                ("output", b.device) for b in scheduler.mapping.bindings(signal)
            )
    return resources


def node_resources(scheduler, node, events):
    resources = set()
    for child in _walk(node):
        resources.update(("qubit", q) for q in child.params.get("operation_qubits", ()))
        if child.kind == "barrier" and child.params.get("qubits"):
            resources.update(qubit_resources(scheduler, child.params["qubits"]))
    for event in events:
        if event.kind == "reserve" and event.signal in scheduler.setup.devices:
            resources.add(("output", event.signal))
        if event.kind in {"rz", "shift_phase", "reset_frame", "reset_phase"}:
            resources.add(("qubit", scheduler.frame_owner(event.signal, event.signal)))
            resources.add(("frame", event.signal))
            signals = event.params.get("target_signals")
            if signals is None:
                signals = [
                    signal
                    for signal in scheduler.mapping
                    if _frame(scheduler, signal) == event.signal
                ]
        else:
            signals = (event.signal,)
        for signal in signals:
            resources.add(("signal", signal))
            if signal in scheduler.mapping:
                resources.update(
                    ("output", binding.device)
                    for binding in scheduler.mapping.bindings(signal)
                )
        if event.kind == "play" and event.params.get("qubit") is not None:
            resources.add(("qubit", event.params["qubit"]))
        if event.kind in {"delay", "reserve"}:
            frame = _frame(scheduler, event.signal)
            if frame is not None:
                resources.add(("qubit", scheduler.frame_owner(frame, frame)))
            else:
                q, _ = scheduler.signal_qubit(event.signal)
                if q is not None:
                    resources.add(("qubit", q.uid))
        if event.kind in {"measure", "prepare"}:
            resources.add(("qubit", event.params["qubit"]))
        resources.update(("qubit", q) for q in event.params.get("qubits", ()))
    return resources


_resources = node_resources
