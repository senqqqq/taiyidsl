"""Validate that a public-field pulse is not mistaken for independent addressing."""

import copy

from ..ir import CompileError


def shared_field_members(root, setup, mapping, available):
    """Resolve only atoms touched by shared outputs used in this experiment."""
    shared = getattr(setup, "shared_outputs", {})
    required = set()

    def visit(node):
        if node.kind in {"play", "trigplay"}:
            for binding in mapping.bindings(node.params["signal"]):
                required.update(shared.get(binding.device, ()))
        for child in node.children:
            visit(child)

    if shared:
        visit(root)
    result = {}
    for signal in sorted(required):
        owners = [q for q in available.values() if signal in q.signals.values()]
        if len(owners) != 1:
            raise CompileError(
                f"Shared signal {signal} needs exactly one registered Qubit owner; "
                "register the affected atoms with LabConfig"
            )
        result[owners[0].uid] = owners[0]
    return result


def validate_shared_fields(root, setup, mapping, qubits):
    """Resolve inferred coverage on the detached compiler tree and check it.

    A shared output is one physical waveform. The reference qubit supplies its
    calibration and frame; the other declared targets are affected, not promised
    to undergo an identical rotation. Legacy setups have no public-field marker.
    """
    shared = getattr(setup, "shared_outputs", {})
    zone_coverage = getattr(setup, "zone_coverage", {})
    owners = {}
    for uid, qubit in qubits.items():
        for signal in qubit.signals.values():
            owners.setdefault(signal, set()).add(uid)

    def visit(node, declaration=None):
        declaration = node.params.get("global_operation", declaration)
        if node.kind in {"play", "trigplay"} and (
            shared or zone_coverage or "global_operation" in node.params
        ):
            signal = node.params["signal"]
            if "global_operation" in node.params:
                outputs = {binding.device for binding in mapping.bindings(signal)}
                if len(outputs) != 1 or any(
                    {binding.device for binding in mapping.bindings(member)} != outputs
                    for member in declaration["signals"]
                ):
                    raise CompileError(
                        "Explicit play targets must share one physical output"
                    )
            for binding in mapping.bindings(signal):
                if binding.device in zone_coverage:
                    # An inherited explicit declaration remains an assertion,
                    # but coverage itself is resolved after each executed move.
                    if declaration is not None:
                        node.params["global_operation"] = copy.deepcopy(declaration)
                    continue
                coverage = set(shared.get(binding.device, ()))
                if len(coverage) < 2:
                    continue
                if declaration is None:
                    if any(len(owners.get(member, ())) != 1 for member in coverage):
                        raise CompileError(
                            "Shared output needs every affected Qubit registered "
                            "with LabConfig or included in the Experiment"
                        )
                    source = [
                        (q.uid, role)
                        for q in qubits.values()
                        for role in q.drive_names
                        if q.signal(role) == signal
                    ]
                    if len(source) != 1:
                        raise CompileError("Shared pulse needs one calibration source")
                    declaration = {
                        "targets": sorted(set().union(*(owners[s] for s in coverage))),
                        "signals": sorted(coverage),
                        "calibration": source[0][0],
                        "drive": source[0][1],
                    }
                    node.params["global_operation"] = declaration
                    node.params["operation_qubits"] = declaration["targets"]
                declared_signals = set(declaration["signals"])
                targets = set(declaration["targets"])
                if declared_signals != coverage or any(
                    not owners.get(member) for member in coverage
                ):
                    raise CompileError(
                        "Global operation must include every configured public-field "
                        "signal and its qubit in the Experiment"
                    )
                affected = set().union(*(owners[member] for member in coverage))
                if targets != affected or declaration["calibration"] not in targets:
                    raise CompileError("Global operation target/calibration mismatch")
                for member in declared_signals:
                    outputs = {b.device for b in mapping.bindings(member)}
                    if outputs != {binding.device}:
                        raise CompileError(
                            "Global operation targets must share one physical output"
                        )
        for child in node.children:
            visit(child, declaration)

    visit(root)
