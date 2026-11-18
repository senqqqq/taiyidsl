"""Associate declared gate words with compiled events, without changing samples.

RB labels consume the frozen notebook metadata contract. They are not inferred
from pulse amplitudes or names. Virtual-Z markers independently consume actual
logical frame events, including events with no following physical pulse.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from fractions import Fraction

from taiyidsl.units import as_fraction


def _require(condition, message):
    if not condition:
        raise ValueError(f"RB waveform annotations: {message}")


def _same_phase(actual, expected):
    return actual is not None and math.isclose(
        math.remainder(float(actual) - float(expected), 2 * math.pi),
        0,
        abs_tol=1e-11,
    )


def _snapshot_number(value, dimension):
    """Decode the notebook's SI number, Quantity or exact Fraction snapshot."""
    if isinstance(value, Mapping):
        if "dimension" in value:
            _require(value["dimension"] == dimension, "pulse snapshot has wrong units")
            value = value["value"]
        else:
            value = Fraction(value["numerator"], value["denominator"])
    return as_fraction(value, dimension)


def _snapshot_duration(pulse):
    if pulse["type"] == "Square":
        return _snapshot_number(pulse["duration"], "time")
    _require(pulse["type"] == "FlatTopGaussian", "unsupported named pulse snapshot")
    return _snapshot_number(pulse["flat_duration"], "time") + (
        2
        * _snapshot_number(pulse["sigma"], "time")
        * _snapshot_number(pulse["cutoff_sigma"], "dimensionless")
    )


def _rb_labels(compiled, point, events):
    metadata = getattr(compiled, "metadata", {})
    if not isinstance(metadata, Mapping):
        return {}
    basis = metadata.get("basis", {})
    if (
        "rb_sequences" not in metadata
        or not isinstance(basis, Mapping)
        or "gates" not in basis
    ):
        return {}
    try:
        reference = metadata["reference_qubit_snapshot"]
        signal = reference["signals"]["rf"]
        spectrum = compiled.setup.spectrum
        frame = spectrum.signal_frames[signal]
        coordinate = (point.coordinates["rb_length"], point.coordinates["rb_sequence"])
        records = [
            record
            for record in metadata["rb_sequences"]
            if (record["length"], record["sequence"]) == coordinate
        ]
        _require(len(records) == 1, "expected one frozen record for this scan point")
        record = records[0]
        _require(
            bool(basis["fingerprint"])
            and record["basis_fingerprint"] == basis["fingerprint"],
            "frozen circuit and basis fingerprints differ",
        )
        indices = [*record["clifford_indices"], record["recovery_index"]]
        words = record["clifford_words"]
        _require(
            len(indices) == len(words) == record["length"] + 1,
            "Clifford words and indices do not have the declared length",
        )
        _require(
            [name for word in words for name in word] == record["native_word"],
            "native word disagrees with the per-Clifford words",
        )
        steps = [
            (
                {
                    "native_gate": name,
                    "clifford_index": index,
                    "clifford_position": position,
                    "recovery": position == record["length"],
                    "implementation_step": step_index,
                },
                step,
            )
            for position, (index, word) in enumerate(zip(indices, words, strict=True))
            for name in word
            for step_index, step in enumerate(basis["gates"][name]["implementation"])
        ]
        # The tutorial resets this frame exactly once at RB entry. Scope the
        # matching to that region, leaving other preparation/control untouched.
        resets = [
            event
            for event in events
            if event.kind == "reset_phase"
            and event.signal == frame
            and event.params.get("route") == "spectrum"
            and signal in event.params.get("target_signals", [])
        ]
        _require(len(resets) == 1, "expected one unambiguous RB frame reset")
        reset = resets[0]
        region = (reset.params["group"], reset.params["region_id"])
        controls = [
            event
            for event in events
            if event.params.get("route") == "spectrum"
            and (event.params.get("group"), event.params.get("region_id")) == region
            and (
                (event.kind in {"play", "trigplay"} and event.signal == signal)
                or (event.kind == "rz" and event.signal == frame)
            )
        ]
        _require(len(steps) == len(controls), "gate steps and compiled events differ")
        labels = {}
        for (label, step), event in zip(steps, controls, strict=True):
            if step["kind"] == "virtual_z":
                _require(event.kind == "rz", "virtual Z does not match a frame event")
                _require(
                    math.isclose(
                        float(event.params["angle"]),
                        float(step["angle"]),
                        rel_tol=0,
                        abs_tol=1e-11,
                    ),
                    "virtual-Z angle disagrees with the frozen implementation",
                )
                _require(event.duration_mu == 0, "virtual Z has nonzero duration")
            else:
                _require(step["kind"] == "rf", "unsupported implementation step")
                _require(
                    event.kind in {"play", "trigplay"},
                    "RF step does not match a pulse event",
                )
                duration = _snapshot_duration(basis["pulses"][step["pulse"]])
                _require(
                    math.isclose(
                        event.duration_mu * float(compiled.setup.tick_seconds),
                        float(duration),
                        rel_tol=1e-10,
                        abs_tol=1e-15,
                    ),
                    "RF duration disagrees with the named pulse snapshot",
                )
                _require(
                    _same_phase(event.params.get("requested_phase"), step["phase"]),
                    "RF base phase disagrees with the frozen implementation",
                )
            _require(bool(event.logical_ids), "compiled control has no logical ID")
            for logical_id in event.logical_ids:
                _require(logical_id not in labels, "compiled logical ID is ambiguous")
                labels[logical_id] = dict(label)
        return labels
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(
            "RB waveform annotations: incomplete frozen gate metadata or event linkage"
        ) from error


def point_gate_annotations(compiled, point):
    """Return ``(labels_by_logical_id, virtual_z_events)`` for one point.

    Labels are attached only when the frozen RB words, named pulse snapshots,
    and compiled control region agree. Inconsistent RB metadata raises rather
    than silently assigning a wrong gate. Other experiments receive no inferred
    gate labels, while their actual Spectrum ``rz`` events still become markers.
    Marker timestamps remain local to ``point.origin_mu``. Original event order,
    coincident updates and terminal recovery Z operations are preserved.
    Missing frame phase values remain ``None``; no phase is reconstructed here.
    """
    events = getattr(point, "logical_events", [])
    labels = _rb_labels(compiled, point, events)
    spectrum = getattr(compiled.setup, "spectrum", None)
    groups = getattr(spectrum, "groups", {})
    virtual_z = []
    for event in events:
        if event.kind != "rz" or event.params.get("route") != "spectrum":
            continue
        params = event.params
        group_name = params.get("group")
        group = groups.get(group_name)
        outputs = getattr(group, "outputs", {})
        channels = {
            (group_name, outputs[name].card, outputs[name].channel)
            for name in params.get("target_devices", [])
            if name in outputs
        }
        virtual_z.append(
            {
                "time_s": (event.start_mu - point.origin_mu)
                * float(compiled.setup.tick_seconds),
                "logical_ids": list(event.logical_ids),
                "signal": event.signal,
                "group": group_name,
                "region_id": params.get("region_id"),
                "angle_rad": params.get("angle"),
                "frame_before_rad": params.get("previous_phase"),
                "frame_after_rad": params.get("effective_frame_phase"),
                "gate_annotations": [
                    dict(labels[logical_id])
                    for logical_id in event.logical_ids
                    if logical_id in labels
                ],
                "channels": [
                    {"group": group, "card": card, "channel": channel}
                    for group, card, channel in sorted(channels)
                ],
            }
        )
    return labels, virtual_z
