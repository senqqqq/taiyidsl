"""Gate labels follow frozen RB words; frame markers follow compiled events."""

import copy
import math
from types import SimpleNamespace

import pytest
from test_yb171_rb_notebook import global_profile, native_definitions
from test_yb171_rb_notebook import pulses as pulses
from test_yb171_rb_notebook import rb as rb

from taiyidsl.reporting.gate_annotations import point_gate_annotations


def editable_copy(compiled):
    """Retain immutable setup objects, copy only metadata and scheduled points."""
    return SimpleNamespace(
        setup=compiled.setup,
        metadata=copy.deepcopy(compiled.metadata),
        points=copy.deepcopy(compiled.points),
    )


@pytest.fixture(scope="module")
def compiled_bases(rb, pulses):
    catalog = rb["build_clifford_catalog"]()
    logical = rb["sample_rb_circuits"](
        [0, 1, 4], sequence_count=2, seed=171, catalog=catalog
    )
    profile, qubit, prepare, readout = global_profile()
    result = {}
    for name in ("xy", "xvz"):
        basis = rb["prepare_gate_basis"](native_definitions(name), pulses)
        decompositions = rb["optimize_clifford_decompositions"](catalog, basis)
        circuits = rb["decompose_rb_circuits"](logical, catalog, decompositions, basis)
        experiment = rb["global_rf_rb"](
            qubit,
            circuits,
            basis,
            pulses,
            lengths=(0, 1, 4),
            sequence_ids=(0, 1),
            prepare=prepare,
            readout=readout,
        )
        result[name] = profile.compile(experiment)
    return result


@pytest.mark.parametrize("basis", ["xy", "xvz"])
def test_rb_point4_gate_labels_zero_length_and_terminal_virtual_z(
    compiled_bases, basis
):
    compiled = compiled_bases[basis]
    before = copy.deepcopy(compiled.to_dict())
    assert point_gate_annotations(compiled, compiled.points[0]) == ({}, [])
    point = compiled.points[4]
    labels, virtual_z = point_gate_annotations(compiled, point)
    circuit = compiled.metadata["rb_sequences"][4]
    controls = [
        event for event in point.logical_events if event.logical_ids[0] in labels
    ]
    assert [
        labels[event.logical_ids[0]]["native_gate"] for event in controls
    ] == circuit["native_word"]
    indices = [19, 15, 13, 1, 19]
    expected_labels = [
        (name, index, position == 4)
        for position, (index, word) in enumerate(
            zip(indices, circuit["clifford_words"], strict=True)
        )
        for name in word
    ]
    assert [
        (label["native_gate"], label["clifford_index"], label["recovery"])
        for label in labels.values()
    ] == expected_labels
    if basis == "xy":
        assert virtual_z == []
        assert sum(label["recovery"] for label in labels.values()) == 2
    else:
        assert [m["gate_annotations"][0]["native_gate"] for m in virtual_z] == [
            "z180",
            "z90",
            "z90",
            "z90",
            "z180",
        ]
        assert [
            m["time_s"] - virtual_z[0]["time_s"] for m in virtual_z
        ] == pytest.approx([0, 0, 400e-9, 800e-9, 1200e-9])
        assert all(
            m["channels"] == [{"group": "qubit_awg", "card": "A", "channel": 0}]
            for m in virtual_z
        )
        assert [m["frame_after_rad"] for m in virtual_z] == pytest.approx(
            [math.radians(d) for d in (-180, -270, -360, -450, -630)]
        )
        assert virtual_z[-1]["gate_annotations"][0]["recovery"] is True
    assert compiled.to_dict() == before
    compiled.assert_integrity()


@pytest.mark.parametrize(
    "corruption,message",
    [
        ("fingerprint", "fingerprints"),
        ("native_word", "native word"),
        ("duration", "RF duration"),
        ("phase", "RF base phase"),
        ("angle", "virtual-Z angle"),
        ("count", "gate steps"),
    ],
)
def test_inconsistent_rb_metadata_cannot_silently_mislabel_pulses(
    compiled_bases, corruption, message
):
    compiled = editable_copy(compiled_bases["xvz"])
    point = compiled.points[4]
    if corruption == "fingerprint":
        compiled.metadata["basis"]["fingerprint"] = "stale"
    elif corruption == "native_word":
        compiled.metadata["rb_sequences"][4]["native_word"][0] = "z90"
    elif corruption == "duration":
        compiled.metadata["basis"]["pulses"]["pi2"]["duration"] = 800e-9
    elif corruption == "phase":
        play = next(e for e in point.logical_events if e.kind == "play")
        play.params["requested_phase"] += 0.25
    elif corruption == "angle":
        event = next(e for e in point.logical_events if e.kind == "rz")
        event.params["angle"] += 0.25
    else:
        point.logical_events = [e for e in point.logical_events if e.kind != "rz"]
    with pytest.raises(ValueError, match=message):
        point_gate_annotations(compiled, point)


@pytest.mark.parametrize("basis", [None, "xy", [], {}])
def test_generic_coincident_virtual_z_deduplicates_outputs_and_keeps_unknown_frame(
    basis,
):
    def event(logical_id, angle):
        return SimpleNamespace(
            kind="rz",
            signal="spin",
            start_mu=50,
            logical_ids=[logical_id],
            params={
                "route": "spectrum",
                "group": "g",
                "region_id": "r",
                "angle": angle,
                "target_devices": ["rf", "rf_alias"],
            },
        )

    output = SimpleNamespace(card="A", channel=0)
    compiled = SimpleNamespace(
        metadata={"basis": basis},
        setup=SimpleNamespace(
            tick_seconds=1e-9,
            spectrum=SimpleNamespace(
                groups={
                    "g": SimpleNamespace(outputs={"rf": output, "rf_alias": output})
                }
            ),
        ),
    )
    point = SimpleNamespace(
        origin_mu=10,
        logical_events=[event("e0", math.pi / 2), event("e1", -math.pi / 2)],
    )
    labels, markers = point_gate_annotations(compiled, point)
    assert labels == {}
    assert [m["logical_ids"] for m in markers] == [["e0"], ["e1"]]
    assert [m["angle_rad"] for m in markers] == [math.pi / 2, -math.pi / 2]
    for marker in markers:
        assert marker["time_s"] == pytest.approx(40e-9)
        assert marker["gate_annotations"] == []
        assert marker["frame_before_rad"] is marker["frame_after_rad"] is None
        assert marker["channels"] == [{"group": "g", "card": "A", "channel": 0}]
    assert point_gate_annotations(compiled, SimpleNamespace()) == ({}, [])


def test_unrelated_control_event_does_not_shift_rb_labels(compiled_bases):
    compiled = editable_copy(compiled_bases["xvz"])
    point = compiled.points[4]
    before = point_gate_annotations(compiled, point)
    foreign = copy.deepcopy(next(e for e in point.logical_events if e.kind == "play"))
    foreign.signal = "q0.clock"
    foreign.logical_ids = ("foreign",)
    point.logical_events.insert(0, foreign)
    assert point_gate_annotations(compiled, point) == before


def test_named_pulse_duration_accepts_float_fraction_and_flat_top_snapshots(
    compiled_bases,
):
    compiled = editable_copy(compiled_bases["xvz"])
    point = compiled.points[4]
    expected = point_gate_annotations(compiled, point)
    pulses = compiled.metadata["basis"]["pulses"]
    pulses["pi2"]["duration"] = 400e-9
    pulses["pi"]["duration"] = {"numerator": 8, "denominator": 10_000_000}
    assert point_gate_annotations(compiled, point) == expected
    pulses["pi2"] = {
        "type": "FlatTopGaussian",
        "flat_duration": {"value": 200e-9, "dimension": "time"},
        "sigma": {"numerator": 5, "denominator": 100_000_000},
        "cutoff_sigma": 2,
    }
    assert point_gate_annotations(compiled, point) == expected
