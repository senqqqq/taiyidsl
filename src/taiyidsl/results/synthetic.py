"""Explicitly synthetic observations for offline examples and analysis checks."""

from __future__ import annotations

import json
from hashlib import sha256

import numpy as np

from .archives import _jsonable
from .scalar import ResultSet


def _probability(model, coordinates, q, parameters):
    x = next(iter(coordinates.values()), 0.0)
    if model == "rabi":
        t = coordinates.get("duration", x)
        pi_duration = parameters.get("pi_duration", q.params.raman.pi_duration)
        if pi_duration <= 0:
            raise ValueError("pi_duration must be positive")
        probability = (
            parameters.get("offset", 0.02)
            + parameters.get("contrast", 0.96)
            * np.sin(np.pi * t / (2 * pi_duration)) ** 2
        )
    elif model == "ramsey":
        tau = coordinates.get("tau", x)
        t2 = parameters.get("t2star", 100e-6)
        if t2 <= 0:
            raise ValueError("t2star must be positive")
        probability = parameters.get("offset", 0.5) + parameters.get(
            "contrast", 0.45
        ) * np.exp(-tau / t2) * np.cos(
            2 * np.pi * parameters.get("detuning", 50e3) * tau
            + parameters.get("phase", 0.0)
        )
    elif model == "rb":
        length = coordinates.get("length", x)
        p = parameters.get("p", 0.99)
        if not 0 <= p <= 1:
            raise ValueError("RB p must lie in [0, 1]")
        probability = parameters.get("amplitude", 0.45) * p**length + parameters.get(
            "offset", 0.5
        )
    else:
        raise ValueError(f"Unknown synthetic model {model!r}")
    if not np.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("Synthetic model produced a probability outside [0, 1]")
    return float(probability)


def synthesize(compiled, *, model=None, seed=0, model_parameters=None, max_shots=None):
    """Generate synthetic classified observations, never real hardware data.

    Randomness is keyed by coordinate, shot, handle and qubit, so averaging
    order does not change synthetic observations. ``max_shots`` bounds executed
    point/shot pairs and leaves remaining observations explicitly unexecuted.
    """
    from ..ir import acquisition_targets

    model = model or compiled.metadata.get("experiment_type", "rabi")
    if model not in {"rabi", "ramsey", "rb"}:
        raise ValueError(f"Unsupported synthetic model {model!r}")
    parameters = dict(model_parameters or {})
    defaults = {
        "rabi": {"offset": 0.02, "contrast": 0.96},
        "ramsey": {"offset": 0.5, "contrast": 0.45, "t2star": 100e-6, "phase": 0.0},
        "rb": {"amplitude": 0.45, "offset": 0.5, "p": 0.99},
    }
    for name, value in defaults[model].items():
        parameters.setdefault(name, value)
    if model == "ramsey":
        parameters.setdefault("detuning", compiled.metadata.get("detuning", 50e3))
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if max_shots is not None and (
        isinstance(max_shots, bool) or not isinstance(max_shots, int) or max_shots < 0
    ):
        raise ValueError("max_shots must be a non-negative integer")
    for name in (
        "missing_probability",
        "loss_probability",
        "loading_failure_probability",
    ):
        if not 0 <= parameters.get(name, 0.0) <= 1:
            raise ValueError(f"{name} must lie in [0, 1]")
    result = ResultSet.from_compiled(
        compiled,
        metadata={
            "synthetic": True,
            "model": model,
            "seed": seed,
            "model_parameters": parameters,
            "experiment_name": compiled.name,
            "experiment_metadata": _jsonable(compiled.metadata),
            "averaging_mode": compiled.averaging_mode,
            "validation_scope": (
                "data and analysis workflow only; not physical performance"
            ),
        },
    )
    for execution_index, (point_index, shot_index) in enumerate(
        compiled.execution_order()
    ):
        if max_shots is not None and execution_index >= max_shots:
            break
        point = compiled.points[point_index]
        result.execution_order.append((point_index, shot_index))
        seen = set()
        observations = (
            (event, uid)
            for event in point.logical_events
            if event.kind in {"measure", "acquire"}
            for uid in acquisition_targets(event)
        )
        for event, uid in observations:
            handle = event.params["handle"]
            if (handle, uid) in seen:
                raise ValueError(
                    "Multiple measurements of the same handle/qubit in one shot "
                    "require distinct handles"
                )
            seen.add((handle, uid))
            key = json.dumps(
                [seed, point_index, point.coordinates, shot_index, handle, uid],
                sort_keys=True,
            )
            entropy = int.from_bytes(sha256(key.encode()).digest()[:16], "little")
            rng = np.random.default_rng(entropy)
            labels = {"loaded": True, "atom_present": True}
            if rng.random() < parameters.get("loading_failure_probability", 0):
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    status="loading_failed",
                    labels={"loaded": False},
                )
            elif rng.random() < parameters.get("loss_probability", 0):
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    status="atom_lost",
                    labels={"loaded": True, "atom_present": False},
                )
            elif rng.random() < parameters.get("missing_probability", 0):
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    status="missing",
                    labels=labels,
                )
            else:
                probability = _probability(
                    model, point.coordinates, compiled.qubits[uid], parameters
                )
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    float(rng.random() < probability),
                    labels=labels,
                )
    return result
