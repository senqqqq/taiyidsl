"""Exercise the tutorial's own RB definitions, without executing its demonstrations."""

import copy
import json
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm
from test_device_descriptor import databases

from taiyidsl import DeviceSetup, LabConfig, NativeKernelPackage, Qubit, Square, ms, ns
from taiyidsl.experiments._single_qubit_clifford import clifford_group, rb_sequences

NOTEBOOK = (
    Path(__file__).resolve().parents[1] / "docs/tutorials/yb171_global_rf_tuneup.ipynb"
)
PAULIS = (
    np.array([[0, 1], [1, 0]], dtype=complex),
    np.array([[0, -1j], [1j, 0]]),
    np.diag([1, -1]),
)


@pytest.fixture(scope="module")
def rb():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    definitions = [
        cell
        for cell in notebook["cells"]
        if {"rb-imports", "rb-definitions"}.intersection(
            cell.get("metadata", {}).get("tags", [])
        )
    ]
    assert definitions, "The notebook must expose stable RB definition-cell tags"
    namespace = {"__name__": "yb171_rb_definitions"}
    # Core + dev installs must exercise the math/DSL without notebook UI extras.
    with pytest.MonkeyPatch.context() as isolated:
        isolated.setitem(sys.modules, "IPython", None)
        isolated.setitem(sys.modules, "IPython.display", None)
        for cell in definitions:
            source = "".join(cell["source"])
            exec(compile(source, f"{NOTEBOOK}:{cell['id']}", "exec"), namespace)
    return namespace


def rotation(axis, angle):
    return expm(-0.5j * angle * PAULIS["xyz".index(axis)])


def product(unitaries):
    result = np.eye(2, dtype=complex)
    for unitary in unitaries:
        result = unitary @ result
    return result


def assert_same_gate(actual, expected):
    relative = actual @ expected.conj().T
    np.testing.assert_allclose(relative, np.trace(relative) / 2 * np.eye(2), atol=1e-11)
    assert abs(np.trace(relative) / 2) == pytest.approx(1)


def action_matrix(action):
    if action["kind"] == "virtual_z":
        return rotation("z", action["angle"])
    axis = np.cos(action["phase"]) * PAULIS[0] + np.sin(action["phase"]) * PAULIS[1]
    return expm(-0.5j * action["angle"] * axis)


def native_matrix(word, definitions):
    return product(
        action_matrix(action)
        for name in word
        for action in definitions[name]["implementation"]
    )


def native_definitions(kind="xvz"):
    definitions = {}
    gates = [
        ("x90", "x", np.pi / 2, 0),
        ("xm90", "x", -np.pi / 2, np.pi),
        ("x180", "x", np.pi, 0),
        ("xm180", "x", -np.pi, np.pi),
    ]
    if kind == "xy":
        gates.extend(
            [
                ("y90", "y", np.pi / 2, np.pi / 2),
                ("ym90", "y", -np.pi / 2, -np.pi / 2),
                ("y180", "y", np.pi, np.pi / 2),
            ]
        )
    for name, axis, angle, phase in gates:
        definitions[name] = {
            "unitary": rotation(axis, angle),
            "implementation": [
                {
                    "kind": "rf",
                    "pulse": "pi" if abs(angle) == np.pi else "pi2",
                    "angle": abs(angle),
                    "phase": phase,
                }
            ],
        }
    if kind == "xvz":
        for name, angle in (("z90", np.pi / 2), ("zm90", -np.pi / 2), ("z180", np.pi)):
            definitions[name] = {
                "unitary": rotation("z", angle),
                "implementation": [{"kind": "virtual_z", "angle": angle}],
            }
    return definitions


@pytest.fixture(scope="module")
def pulses():
    return {
        "pi2": Square(400 * ns, amplitude=0.2),
        "pi": Square(800 * ns, amplitude=0.2),
    }


def bellman_ford(catalog, basis):
    """Independent bounded relaxation of all graph edges, not a priority queue."""
    lookup = {tuple(entry["rotation"].flat): i for i, entry in enumerate(catalog)}
    distances = [None] * len(catalog)
    distances[0] = (0, Fraction(0), 0)
    for _ in range(len(catalog) - 1):
        previous = distances.copy()
        for index, entry in enumerate(catalog):
            if previous[index] is None:
                continue
            for gate in basis.values():
                target = lookup[tuple((gate["rotation"] @ entry["rotation"]).flat)]
                cost = tuple(a + b for a, b in zip(previous[index], gate["cost"]))
                if distances[target] is None or cost < distances[target]:
                    distances[target] = cost
    return distances


def test_catalog_retains_indices_and_exact_pauli_action(rb):
    catalog = rb["build_clifford_catalog"]()
    reference = clifford_group()
    assert len(catalog) == 24
    for entry, (expected_rotation, expected_word) in zip(
        catalog, reference, strict=True
    ):
        np.testing.assert_array_equal(entry["rotation"], expected_rotation)
        assert tuple(entry["reference_word"]) == expected_word
        unitary = product(rb["RF_REFERENCE_UNITARIES"][n] for n in expected_word)
        for column, pauli in enumerate(PAULIS):
            conjugated = sum(
                entry["rotation"][row, column] * p for row, p in enumerate(PAULIS)
            )
            np.testing.assert_allclose(
                unitary @ pauli @ unitary.conj().T, conjugated, atol=1e-12
            )
        np.testing.assert_array_equal(
            rb["unitary_to_rotation"](unitary), expected_rotation
        )
        assert rb["same_unitary"](unitary, np.exp(0.37j) * unitary)
    np.testing.assert_allclose(
        rb["rb_rotation"]("x", np.pi / 2), rotation("x", np.pi / 2)
    )
    np.testing.assert_allclose(
        rb["chronological_unitary"](
            [rotation("x", np.pi / 2), rotation("y", np.pi / 2)]
        ),
        rotation("y", np.pi / 2) @ rotation("x", np.pi / 2),
        atol=1e-12,
    )


@pytest.mark.parametrize("kind", ["xy", "xvz"])
def test_all_decompositions_match_unitaries_and_independent_optimum(rb, pulses, kind):
    catalog = rb["build_clifford_catalog"]()
    definitions = native_definitions(kind)
    basis = rb["prepare_gate_basis"](definitions, pulses)
    decompositions = rb["optimize_clifford_decompositions"](catalog, basis)
    assert len(decompositions) == 24
    assert [entry["cost"] for entry in decompositions] == bellman_ford(catalog, basis)
    assert tuple(decompositions[0]["native_word"]) == ()
    for entry, decomposition in zip(catalog, decompositions, strict=True):
        target = product(
            rb["RF_REFERENCE_UNITARIES"][n] for n in entry["reference_word"]
        )
        assert_same_gate(
            native_matrix(decomposition["native_word"], definitions), target
        )
    if kind == "xvz":
        z90 = next(
            i
            for i, entry in enumerate(catalog)
            if np.array_equal(entry["rotation"], basis["z90"]["rotation"])
        )
        assert decompositions[z90]["cost"] == (0, Fraction(0), 1)


def test_cost_uses_pulse_count_exact_duration_and_stable_ties(rb):
    pulses = {
        "pi2": Square(1.0, amplitude=0.2),
        "pi": Square(2.0, amplitude=0.2),
        "tenth": Square(0.1, amplitude=0.2),
        "fifth": Square(0.2, amplitude=0.2),
        "three_twentieths": Square(0.15, amplitude=0.2),
    }
    definitions = native_definitions()
    for name, pulse_names in (
        ("decimal_first", ("tenth", "fifth")),
        ("decimal_second", ("three_twentieths", "three_twentieths")),
    ):
        definitions[name] = {
            "unitary": rotation("x", np.pi),
            "implementation": [
                {"kind": "rf", "pulse": pulse, "angle": np.pi / 2, "phase": 0}
                for pulse in pulse_names
            ],
        }
    basis = rb["prepare_gate_basis"](definitions, pulses)
    assert (
        basis["decimal_first"]["cost"]
        == basis["decimal_second"]["cost"]
        == (2, Fraction(3, 10), 2)
    )
    catalog = rb["build_clifford_catalog"]()
    x180 = next(
        i
        for i, entry in enumerate(catalog)
        if np.array_equal(entry["rotation"], basis["x180"]["rotation"])
    )
    decomposition = rb["optimize_clifford_decompositions"](catalog, basis)[x180]
    assert tuple(decomposition["native_word"]) == (
        "x180",
    )  # One pulse beats two shorter ones; +pi wins the +/-pi tie.
    basis.pop("x180")
    basis.pop("xm180")
    decomposition = rb["optimize_clifford_decompositions"](catalog, basis)[x180]
    assert tuple(decomposition["native_word"]) == ("decimal_first",)
    assert decomposition["cost"] == (2, Fraction(3, 10), 2)


def test_invalid_matrices_implementations_and_incomplete_basis_are_rejected(rb, pulses):
    for unitary in (np.diag([1, 2]), rotation("z", np.pi / 4), np.full((2, 2), np.nan)):
        with pytest.raises(ValueError):
            rb["unitary_to_rotation"](unitary)
    definitions = native_definitions()
    wrong = copy.deepcopy(definitions)
    wrong["x90"]["implementation"][0]["phase"] = np.pi / 2
    with pytest.raises(ValueError):
        rb["prepare_gate_basis"](wrong, pulses)
    incomplete = rb["prepare_gate_basis"]({"x90": definitions["x90"]}, pulses)
    with pytest.raises(ValueError, match="24"):
        rb["optimize_clifford_decompositions"](
            rb["build_clifford_catalog"](), incomplete
        )


def test_frozen_indices_are_basis_independent_and_recovery_is_complete(rb, pulses):
    catalog = rb["build_clifford_catalog"]()
    circuits = rb["sample_rb_circuits"](
        [0, 1, 4], sequence_count=3, seed=171, catalog=catalog
    )
    legacy = rb_sequences([0, 1, 4], sequence_count=3, seed=171)
    for circuit, old in zip(circuits, legacy, strict=True):
        assert list(circuit["clifford_indices"]) == old["clifford_indices"]
        assert circuit["recovery_index"] == old["recovery_index"]
    reordered = rb["sample_rb_circuits"](
        [4, 0, 1], sequence_count=4, seed=171, catalog=catalog
    )
    existing = {(r["length"], r["sequence"]): r for r in circuits}
    assert all(
        existing[(r["length"], r["sequence"])] == r
        for r in reordered
        if r["sequence"] < 3
    )
    for kind in ("xy", "xvz"):
        definitions = native_definitions(kind)
        basis = rb["prepare_gate_basis"](definitions, pulses)
        decompositions = rb["optimize_clifford_decompositions"](catalog, basis)
        expanded = rb["decompose_rb_circuits"](circuits, catalog, decompositions, basis)
        for circuit, actual in zip(circuits, expanded, strict=True):
            assert actual["clifford_indices"] == circuit["clifford_indices"]
            assert actual["recovery_index"] == circuit["recovery_index"]
            assert_same_gate(
                native_matrix(actual["native_word"], definitions), np.eye(2)
            )
            assert len(actual["clifford_words"]) == circuit["length"] + 1
            if circuit["length"] == 0:
                assert not actual["native_word"]


def test_frozen_circuits_reject_same_cost_basis_and_pulse_changes(rb, pulses):
    catalog = rb["build_clifford_catalog"]()
    definitions = native_definitions()
    basis = rb["prepare_gate_basis"](definitions, pulses)
    decompositions = rb["optimize_clifford_decompositions"](catalog, basis)
    logical = rb["sample_rb_circuits"]([1], sequence_count=1, seed=171, catalog=catalog)
    circuits = rb["decompose_rb_circuits"](logical, catalog, decompositions, basis)

    changed_definitions = copy.deepcopy(definitions)
    changed_definitions["x90"]["unitary"] = rotation("y", np.pi / 2)
    changed_definitions["x90"]["implementation"][0]["phase"] = np.pi / 2
    changed_basis = rb["prepare_gate_basis"](changed_definitions, pulses)
    assert changed_basis["x90"]["cost"] == basis["x90"]["cost"]

    changed_pulses = {
        **pulses,
        "pi2": Square(pulses["pi2"].duration, amplitude=0.3),
    }
    for current_basis, current_pulses in (
        (changed_basis, pulses),
        (basis, changed_pulses),
    ):
        with pytest.raises(ValueError):
            rb["global_rf_rb"](
                Qubit("q"),
                circuits,
                current_basis,
                current_pulses,
                lengths=(1,),
                sequence_ids=(0,),
                prepare=lambda: None,
                readout=lambda: None,
                shots=1,
            )


def global_profile():
    descriptor, device_db, spec_db = databases()
    for channel, name in enumerate(("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"), start=4):
        device_db[name] = {
            "type": "local",
            "module": "artiq.coredevice.ttl",
            "class": "TTLOut",
            "arguments": {"channel": channel},
        }
        descriptor["channels"][name] = {"instrument": "artiq", "device": name}
    descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
    descriptor["signals"] = {
        **{f"{name}.rf": "nuclear_rf" for name in ("q0", "q1", "spectator")},
        "readout": "camera",
    }
    qubits = [
        Qubit(
            name,
            signals={"rf": f"{name}.rf"},
            parameters={"rf": {"frequency": 1e6, "frame": "spin"}},
        )
        for name in ("q0", "q1", "spectator")
    ]
    profile = LabConfig(
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db),
        qubits=qubits,
    )
    package = NativeKernelPackage.from_package(
        root=NOTEBOOK.parents[2] / "examples/artiq_spectrum/greenmot_native",
        entry="greenmot_fixture:GreenMOTFixture",
        resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
        initialize_method="initialize_native",
        finalize_method="finalize_native",
    )
    prefix = package.kernel("prepare_MOT", timing="prefix")
    tail = package.kernel(
        "finish_preparation",
        timing="timed",
        duration=15 * ms + 16 * ns,
        end_boundary="OFFLINE fixture TTL boundary; no atoms or transport",
    )

    def prepare():
        prefix()
        tail()

    readout = package.kernel("execute_readout", timing="suffix", duration=100.48 * ms)
    return profile, qubits[0], prepare, readout


def test_small_sweep_plays_global_rf_once_and_preserves_virtual_frames(rb, pulses):
    catalog = rb["build_clifford_catalog"]()
    basis = rb["prepare_gate_basis"](native_definitions(), pulses)
    decompositions = rb["optimize_clifford_decompositions"](catalog, basis)
    logical = rb["sample_rb_circuits"](
        [0, 1, 4], sequence_count=2, seed=171, catalog=catalog
    )
    circuits = rb["decompose_rb_circuits"](logical, catalog, decompositions, basis)
    profile, q, prepare, readout = global_profile()
    experiment = rb["global_rf_rb"](
        q,
        circuits,
        basis,
        pulses,
        lengths=(0, 1, 4),
        sequence_ids=(0, 1),
        prepare=prepare,
        readout=readout,
        shots=2,
    )
    compiled = profile.compile(experiment)
    assert [p.axis_indices for p in compiled.points] == [
        (i, j) for i in range(3) for j in range(2)
    ]
    assert len(list(compiled.execution_order())) == 12
    saw_virtual = False
    for index, (point, circuit) in enumerate(
        zip(compiled.points, circuits, strict=True)
    ):
        plays = [
            e for e in point.logical_events if e.kind == "play" and e.signal == q.rf
        ]
        native = compiled.metadata["native_artiq"]["points"][index]
        assert [call["method"] for call in native["prefix"]] == ["prepare_MOT"]
        assert [call["method"] for call in native["timed"]] == ["finish_preparation"]
        assert [call["method"] for call in native["suffix"]] == ["execute_readout"]
        resets = [e for e in point.logical_events if e.kind == "reset_phase"]
        assert len(resets) == 1
        actions = [
            action
            for name in circuit["native_word"]
            for action in basis[name]["implementation"]
        ]
        expected_phases, frame = [], 0.0
        for action in actions:
            if action["kind"] == "virtual_z":
                saw_virtual = True
                frame -= action["angle"]
            else:
                expected_phases.append(action["phase"] + frame)
        assert len(plays) == len(expected_phases)
        np.testing.assert_allclose(
            [e.params["phase"] for e in plays], expected_phases, atol=1e-12
        )
        assert all(
            e.params["global_operation"]["targets"] == ["q0", "q1", "spectator"]
            for e in plays
        )
        if circuit["length"] == 0:
            assert not plays
            assert not any(e.kind == "spectrum_program" for e in point.physical_events)
    assert saw_virtual


def test_pure_virtual_circuit_and_terminal_virtual_z_remain_visible(rb, pulses):
    catalog = rb["build_clifford_catalog"]()
    basis = rb["prepare_gate_basis"](native_definitions(), pulses)
    decompositions = rb["optimize_clifford_decompositions"](catalog, basis)
    lookup = {tuple(entry["rotation"].flat): i for i, entry in enumerate(catalog)}
    logical = [
        {
            "seed": 171,
            "length": 1,
            "sequence": i,
            "clifford_indices": [i],
            "recovery_index": lookup[tuple(entry["rotation"].T.flat)],
        }
        for i, entry in enumerate(catalog)
    ]
    circuits = rb["decompose_rb_circuits"](logical, catalog, decompositions, basis)
    actions = [
        [
            action
            for name in row["native_word"]
            for action in basis[name]["implementation"]
        ]
        for row in circuits
    ]
    pure = next(
        i
        for i, word in enumerate(actions)
        if word and all(a["kind"] == "virtual_z" for a in word)
    )
    mixed = next(
        i
        for i, word in enumerate(actions)
        if word
        and word[-1]["kind"] == "virtual_z"
        and any(a["kind"] == "rf" for a in word)
    )
    selected = [dict(circuits[i], sequence=j) for j, i in enumerate((pure, mixed))]
    profile, q, prepare, readout = global_profile()
    compiled = profile.compile(
        rb["global_rf_rb"](
            q,
            selected,
            basis,
            pulses,
            lengths=(1,),
            sequence_ids=(0, 1),
            prepare=prepare,
            readout=readout,
            shots=1,
        )
    )
    for point, index in zip(compiled.points, (pure, mixed), strict=True):
        word = actions[index]
        virtual = [e for e in point.logical_events if e.kind == "rz"]
        np.testing.assert_allclose(
            [e.params["angle"] for e in virtual],
            [a["angle"] for a in word if a["kind"] == "virtual_z"],
        )
        plays = [
            e for e in point.logical_events if e.kind == "play" and e.signal == q.rf
        ]
        if index == pure:
            assert not plays
        else:
            assert virtual[-1].start_mu == plays[-1].end_mu


def test_fit_is_local_and_distinguishes_unidentifiable_data(rb):
    depths = np.array([0, 1, 4, 8, 16, 32, 64, 128])
    fit = rb["fit_rb_decay"](depths, 0.47 * 0.993**depths + 0.50)
    assert fit["p"] == pytest.approx(0.993, abs=1e-7)
    assert fit["residual_rms"] < 1e-8
    with pytest.raises(ValueError):
        rb["fit_rb_decay"](depths, np.full_like(depths, 0.5, dtype=float))
