"""Focused public authoring checks: sections, native registration and Ramsey."""

import math
import sys
from pathlib import Path

import pytest
from test_section_scheduling import drives, gate, lab_for

from taiyidsl import (
    DriveParameters,
    Experiment,
    LabConfig,
    NativeKernelPackage,
    Qubit,
    QubitParameters,
    SweepParameter,
    layer,
    ns,
    section,
)
from taiyidsl.experiments.ramsey import ramsey_experiment
from taiyidsl.units import deg, kHz

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("align", ["start", "end"])
def test_parallel_section_preserves_layer_alignment_and_asap_boundary(align):
    def build(use_layer):
        q0, q1 = Qubit("q0"), Qubit("q1")
        exp = Experiment("aligned", [q0, q1])
        with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
            with section(schedule="asap"):
                gate(q0, 400 * ns)
                context = (
                    layer(q0, q1, align=align)
                    if use_layer
                    else section(
                        "layer", schedule="parallel", align=align, qubits=(q0, q1)
                    )
                )
                with context as saved:
                    with section("composite"):
                        gate(q0, 400 * ns)
                        gate(q0, 400 * ns)
                    gate(q1, 400 * ns)
                gate(q1, 400 * ns)
        assert saved.schedule == "parallel" and saved.align == align
        return [
            (e.signal, e.start_mu, e.duration_mu)
            for e in drives(lab_for().compile(exp))
        ]

    old, new = build(True), build(False)
    assert new == old
    assert new[0][1] == 0 and new[-1][1] == 6000
    with pytest.raises(ValueError, match="only defined"):
        with section(align="end"):
            pass


def test_native_package_discovery_pins_relative_dependencies_without_import(tmp_path):
    package = tmp_path / "my_lab"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('must not import')\n")
    (package / "helper.py").write_text("class Base: pass\n")
    (package / "stages.py").write_text(
        "from .helper import Base\n"
        "from artiq.experiment import kernel\n"
        "class Stages(Base):\n"
        "    @kernel\n"
        "    def prepare(self): pass\n"
    )
    native = NativeKernelPackage.from_package(
        root=tmp_path, entry="my_lab.stages:Stages", resources=("ttl",)
    )
    assert {pin["module"] for pin in native.to_dict()["sources"]} == {
        "my_lab",
        "my_lab.helper",
        "my_lab.stages",
    }
    assert "my_lab" not in sys.modules
    with pytest.raises(ValueError, match="Expected one class"):
        NativeKernelPackage.from_package(
            root=tmp_path, entry="my_lab.stages:Missing", resources=("ttl",)
        )
    (package / "helper.py").unlink()
    with pytest.raises(ValueError, match="Missing local"):
        NativeKernelPackage.from_package(
            root=tmp_path, entry="my_lab.stages:Stages", resources=("ttl",)
        )


def test_ramsey_sweep_frequency_phase_and_native_boundaries():
    from examples.artiq_spectrum.greenmot_native.adapter import GreenMOTAdapter

    q = Qubit(
        "q",
        QubitParameters(
            rf=DriveParameters(
                frequency=30_000,
                amplitude=0.2,
                pi_duration=800 * ns,
                pi2_duration=400 * ns,
            )
        ),
    )
    snapshot = q.to_dict()
    exp = ramsey_experiment(
        qubit=q,
        native=GreenMOTAdapter(),
        delays=SweepParameter("tau", [800, 1600], "ns"),
        detuning=SweepParameter("detuning", [-1, 1], "kHz"),
        second_phase=SweepParameter("phase", [0, 90], "deg"),
    )
    compiled = LabConfig.load(
        ROOT / "examples/artiq_spectrum/lab_simulated.json"
    ).compile(exp)
    assert [a["name"] for a in compiled.axes] == ["tau", "detuning", "phase"]
    assert q.to_dict() == snapshot
    for point in compiled.points:
        coordinates = point.coordinates
        pulses = next(
            e.params["cards"]["A"]["program"]["pulses"]
            for e in point.physical_events
            if e.kind == "spectrum_program"
        )
        assert len(pulses) == 2
        frequency = 30_000 + coordinates["detuning"]
        assert [p["frequency_hz"] for p in pulses] == [frequency, frequency]
        gap = (
            pulses[1]["start_sample"]
            - pulses[0]["start_sample"]
            - pulses[0]["duration_samples"]
        ) / 1.25e9
        assert gap == pytest.approx(coordinates["tau"])
        assert pulses[0]["phase_rad"] == pytest.approx(0)
        expected = (
            2 * math.pi * frequency * (400e-9 + coordinates["tau"])
            + coordinates["phase"]
        ) % (2 * math.pi)
        assert pulses[1]["phase_rad"] == pytest.approx(expected)
    assert compiled.metadata["detuning_mode"] == "drive_frequency_offset"


def test_scalar_ramsey_controls_and_invalid_frequency():
    from examples.artiq_spectrum.greenmot_native.adapter import GreenMOTAdapter

    q = Qubit("q", QubitParameters(rf=DriveParameters(frequency=30_000)))
    native = GreenMOTAdapter()
    exp = ramsey_experiment(
        qubit=q,
        native=native,
        delays=800 * ns,
        detuning=1 * kHz,
        second_phase=90 * deg,
    )
    direct = ramsey_experiment(
        qubit=q,
        prepare=native.prepare_MOT,
        readout=native.readout,
        delays=800 * ns,
        detuning=1 * kHz,
        second_phase=90 * deg,
    )
    assert direct.root == exp.root
    assert direct.metadata == exp.metadata and direct.qubits == exp.qubits
    compiled = LabConfig.load(
        ROOT / "examples/artiq_spectrum/lab_simulated.json"
    ).compile(exp)
    assert len(compiled.points) == 1 and compiled.axes == []
    with pytest.raises(ValueError, match="Ramsey drive frequency"):
        ramsey_experiment(qubit=q, native=native, delays=800 * ns, detuning=-31 * kHz)
    with pytest.raises(ValueError, match="not both"):
        ramsey_experiment(
            qubit=q, native=native, prepare=native.prepare_MOT, delays=800 * ns
        )
    for hooks in (
        {},
        {"prepare": native.prepare_MOT},
        {"readout": native.readout},
        {"prepare": native.prepare_MOT, "readout": object()},
    ):
        with pytest.raises(TypeError, match="both be declared DSL callables"):
            ramsey_experiment(qubit=q, delays=800 * ns, **hooks)
