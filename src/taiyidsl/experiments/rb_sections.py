"""Reusable bridge from frozen ideal RB descriptors to laboratory gate Sections."""

from ..dsl.sections import layer, section
from .rb_sequences import RBSequence


def rb_sequence_section(qubits, sequence, operations):
    """Build/attach a Section without redrawing random gates or resetting frames.

    ``operations`` supplies calibrated or explicitly illustrative named gate
    factories. Each primitive forms a complete circuit layer on the declared
    register. The logical CZ role does not certify the supplied pulse body.
    Averaging, preparation, trigger ownership and readout stay with the caller.

    Gate names are an interface to the laboratory implementation, not a demand
    for one physical pulse per name. For single-qubit RB, operations must supply
    ``x90``, ``xm90``, ``y90``, ``ym90``, ``x180`` and ``y180`` methods accepting
    one qubit. They may emit DSL directly or be ``qubit_operation`` factories
    that automatically attach their Sections in the active context. A plain
    factory that only returns an unattached Section is not inserted here.

    For example, an X/virtual-Z implementation of logical Y(theta) emits, in
    execution order, ``rz(q, -pi/2)``, X(theta), ``rz(q, +pi/2)``. Their product
    is ``Rz(+pi/2) @ Rx(theta) @ Rz(-pi/2) = Ry(theta)``. Implement y90, ym90 and
    y180 with theta = +pi/2, -pi/2 and pi respectively, using the same RF/Raman
    frame as rz. Do not reset the frame between these operations. This changes
    the native realization while preserving the sampled Clifford indices and
    ideal recovery. It does not re-optimize whole Cliffords for the new basis;
    descriptor gate counts still count logical primitives, not emitted pulses.
    """
    qubits = tuple(qubits)
    if not isinstance(sequence, RBSequence) or len(qubits) != sequence.num_qubits:
        raise ValueError("RB descriptor and qubit register must have the same size")
    if len({q.uid for q in qubits}) != len(qubits):
        raise ValueError("RB register qubits must be distinct")
    name = f"rb_{sequence.length}_{sequence.sequence}_{sequence.variant}"
    with section(name, schedule="asap") as body:
        for index, gate in enumerate(sequence.gates):
            with layer(*qubits, name=f"primitive_{index}"):
                getattr(operations, gate.name)(*(qubits[q] for q in gate.targets))
    return body
