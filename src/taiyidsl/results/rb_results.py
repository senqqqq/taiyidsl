"""Joint, same-shot RB outcome counts with explicit loss/invalid-data policies.

Raw ResultSet observations remain the source of truth. In particular, P00 is
not the product of independently averaged single-qubit populations. A physical
loss is not automatically a spin measurement or silently postselected away.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

_POLICIES = {"error", "count_as_failure", "exclude"}
_LOSS = {"atom_lost", "loading_failed"}


@dataclass(frozen=True)
class RBCounts:
    length: int
    sequence: int
    variant: str
    qubits: tuple[str, ...]
    counts: tuple[int, ...]
    lost: int = 0
    invalid: int = 0
    loss_policy: str = "error"
    invalid_policy: str = "error"
    status_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "qubits", tuple(self.qubits))
        object.__setattr__(self, "counts", tuple(self.counts))
        object.__setattr__(self, "status_counts", tuple(self.status_counts))
        if len(self.qubits) not in (1, 2) or len(set(self.qubits)) != len(self.qubits):
            raise ValueError("Joint RB counts require one or two distinct qubits")
        if self.variant not in {"reference", "interleaved"}:
            raise ValueError("RB variant must be reference or interleaved")
        if self.variant == "interleaved" and len(self.qubits) != 2:
            raise ValueError("CZ interleaved counts require two qubits")
        if len(self.counts) != 2 ** len(self.qubits):
            raise ValueError("Joint counts must include every bitstring")
        for value in (
            self.length,
            self.sequence,
            *self.counts,
            self.lost,
            self.invalid,
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    "RB coordinates and counts must be nonnegative integers"
                )
        if self.loss_policy not in _POLICIES or self.invalid_policy not in _POLICIES:
            raise ValueError(f"Outcome policy must be one of {sorted(_POLICIES)}")
        if self.lost and self.loss_policy == "error":
            raise ValueError("RB contains loss; choose an explicit loss_policy")
        if self.invalid and self.invalid_policy == "error":
            raise ValueError(
                "RB contains invalid shots; choose an explicit invalid_policy"
            )

    @property
    def shots(self):
        return sum(self.counts) + self.lost + self.invalid

    @property
    def denominator(self):
        return (
            sum(self.counts)
            + (self.lost if self.loss_policy == "count_as_failure" else 0)
            + (self.invalid if self.invalid_policy == "count_as_failure" else 0)
        )

    @property
    def probability(self):
        return self.counts[0] / self.denominator if self.denominator else float("nan")

    def to_dict(self):
        n = len(self.qubits)
        return {
            "length": self.length,
            "sequence": self.sequence,
            "variant": self.variant,
            "qubits": list(self.qubits),
            "counts": {
                format(i, f"0{n}b"): value for i, value in enumerate(self.counts)
            },
            "lost": self.lost,
            "invalid": self.invalid,
            "shots": self.shots,
            "denominator": self.denominator,
            "excluded": self.shots - self.denominator,
            "loss_policy": self.loss_policy,
            "invalid_policy": self.invalid_policy,
            "joint_status_counts": dict(self.status_counts),
            "p_zero": self.probability if self.denominator else None,
        }


def joint_rb_counts(
    results,
    handle,
    *,
    qubits,
    loss_policy="error",
    invalid_policy="error",
    variant_labels=None,
):
    """Count per-sequence joint outcomes from classified ResultSet bits.

    Coordinates must have length/sequence and optional variant (reference by
    default). Numeric variant sweeps require an explicit ``variant_labels`` map.
    Select any other varying experiment axes before calling. Bit zero
    means the desired prepared/recovered state; callers must classify images
    explicitly. Invalid has precedence over loss in a mixed-status shot; the
    complete ordered status combinations are retained as diagnostics.
    """
    qubits = tuple(qubits)
    if len(qubits) not in (1, 2) or len(set(qubits)) != len(qubits):
        raise ValueError("Select one or two distinct qubits for joint RB results")
    if loss_policy not in _POLICIES or invalid_policy not in _POLICIES:
        raise ValueError(f"Outcome policy must be one of {sorted(_POLICIES)}")
    indices = [results.qubits.index(q) for q in qubits]
    extra = set().union(*(coord.keys() for coord in results.coordinates)) - {
        "length",
        "sequence",
        "variant",
    }
    if any(len({coord.get(key) for coord in results.coordinates}) > 1 for key in extra):
        raise ValueError("Select a single slice of extra RB result axes first")
    output, seen = [], set()
    for point, coord in enumerate(results.coordinates):
        length, sequence = coord.get("length"), coord.get("sequence")
        if any(
            isinstance(v, (bool, np.bool_))
            or not isinstance(v, (int, float, np.integer, np.floating))
            or not np.isfinite(v)
            or int(v) != v
            for v in (length, sequence)
        ):
            raise ValueError(
                "RB results require integer length and sequence coordinates"
            )
        variant = coord.get("variant", "reference")
        if variant_labels is not None:
            if variant not in variant_labels:
                raise ValueError("RB variant is absent from the explicit label mapping")
            variant = variant_labels[variant]
        key = (int(length), int(sequence), variant)
        if key in seen:
            raise ValueError("Duplicate RB condition; merge raw shots explicitly")
        seen.add(key)
        counts, lost, invalid = [0] * (2 ** len(qubits)), 0, 0
        statuses = Counter()
        values = results.data[handle][point][:, indices]
        states = results.status[handle][point][:, indices]
        for bits, status in zip(values, states):
            statuses["/".join(status)] += 1
            if any(item != "valid" and item not in _LOSS for item in status):
                invalid += 1
            elif any(item in _LOSS for item in status):
                lost += 1
            elif not np.all(np.isin(bits, (0, 1))):
                raise ValueError("Valid RB observations must be classified bits 0 or 1")
            else:
                outcome = 0
                for bit in bits:
                    outcome = 2 * outcome + int(bit)
                counts[outcome] += 1
        output.append(
            RBCounts(
                *key,
                qubits,
                tuple(counts),
                lost,
                invalid,
                loss_policy,
                invalid_policy,
                tuple(sorted(statuses.items())),
            )
        )
    return tuple(output)
