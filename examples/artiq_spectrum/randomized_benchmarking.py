"""Offline 1q RB or full 2q Clifford RB paired with CZ interleaved RB.

The ideal circuit algebra is exact. ``demonstration_cz`` is an explicit pair of
clock-drive waveforms used to exercise compilation and replaceable Section
composition. It is NOT a physical CZ model or a claim of a calibrated gate.
Supply laboratory QubitOperations and qubit snapshots to build_experiment for
real pulse definitions. This script only exports; it never runs hardware.
"""

import argparse
import json
from pathlib import Path

import numpy as np

from taiyidsl import (
    Device,
    DeviceSetup,
    DriveParameters,
    Experiment,
    LabConfig,
    PairCalibration,
    Qubit,
    QubitOperations,
    QubitParameters,
    ResultSet,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    SweepParameter,
    acquire,
    delay,
    ns,
    parallel,
    play,
    qubit_operation,
    reset_frame,
    us,
)
from taiyidsl.rb import CATALOG_VERSION, generate_rb_sequences
from taiyidsl.rb_dsl import rb_sequence_section
from taiyidsl.results.rb_analysis import analyze_rb
from taiyidsl.results.rb_results import joint_rb_counts
from taiyidsl.units import as_fraction

try:
    from examples.artiq_spectrum.rb_native import build_rb_native_program
except ModuleNotFoundError as error:
    if error.name != "examples":
        raise
    from rb_native import build_rb_native_program

VARIANTS = {0: "reference", 1: "interleaved"}


def build_lab(num_qubits=2):
    """Two cards, each with nuclear RF and a separate clock output/phase frame."""
    if type(num_qubits) is not int or num_qubits not in (1, 2):
        raise ValueError("The example supports one or two qubits")
    group = SpectrumGroup(
        cards={name: SpectrumCard(f"SIM_RB_{name}") for name in ("A", "B")},
        outputs={
            f"q{i}_{drive}": SpectrumOutput(card, channel)
            for i, card in enumerate(("A", "B"))
            for channel, drive in enumerate(("rf", "clock"))
        },
        trigger_devices={"A": "awg_trigger_a", "B": "awg_trigger_b"},
        calibration={"status": "unmeasured", "revision": "rb-offline-demo"},
    )
    setup = DeviceSetup(
        {
            **{name: Device(name, "spectrum") for name in group.outputs},
            **{
                name: Device(name, "ttl", grid_mu=1)
                for name in (
                    "awg_trigger_a",
                    "awg_trigger_b",
                    "camera_ttl",
                )
            },
        },
        spectrum=SpectrumConfig({"qubit_awg": group}),
    )
    signals = SignalMap(
        {
            **{
                f"q{i}.{drive}": f"q{i}_{drive}"
                for i in range(num_qubits)
                for drive in ("rf", "clock")
            },
            "camera.trigger": "camera_ttl",
        }
    )
    return LabConfig(setup, signals, backend="artiq_spectrum")


def example_qubits(num_qubits):
    return tuple(
        Qubit(
            f"q{i}",
            QubitParameters(
                rf=DriveParameters(
                    frequency=10e6 + i * 2e6,
                    amplitude=0.3,
                    pi_duration=800e-9,
                    pi2_duration=400e-9,
                ),
                drives={
                    "clock": DriveParameters(
                        frequency=100e6 + i * 10e6,
                        amplitude=0.12,
                        pi_duration=800e-9,
                        pi2_duration=400e-9,
                    )
                },
                drive_frames={"clock": "clock"},
            ),
        )
        for i in range(num_qubits)
    )


@qubit_operation
def demonstration_cz(q0, q1):
    """Uncalibrated simultaneous clock pulses; replace this entire Section body.

    The ideal RB generator assigns the CZ matrix to this operation's *intended*
    logical role. These two ordinary pulses do not establish entanglement. A
    laboratory body must include the actual interaction, pulse parameters and
    measured single-qubit phase corrections required by that implementation.
    """
    with parallel():
        for q in (q0, q1):
            drive = q.drive("clock")
            play(
                q.signal("clock"),
                Square(800 * ns, amplitude=drive.amplitude, frequency=drive.frequency),
            )


def example_operations(qubits):
    pair = ()
    if len(qubits) == 2:
        pair = (
            PairCalibration(
                tuple(q.uid for q in qubits),
                "demonstration_cz",
                status="demonstration",
                parameters={"physical_cz_verified": False, "duration_s": 800e-9},
                qubit_revisions={q.uid: q.revision for q in qubits},
                qubit_fingerprints={q.uid: q.fingerprint for q in qubits},
                source="explicit offline pulse illustration; not a CZ calibration",
            ),
        )
    return QubitOperations(drive="rf", cz_impl=demonstration_cz, pair_calibrations=pair)


def build_experiment(
    *,
    num_qubits=2,
    lengths=(0, 1, 4, 12),
    sequence_count=3,
    seed=101,
    shots=128,
    chunks=2,
    interleaved=None,
    qubits=None,
    operations=None,
    native_program=None,
    preparation_to_gate=1 * us,
):
    """Freeze random circuits once; sweep length/sequence/paired variant.

    For a fixed random seed, changing chunks/averaging/shots leaves all gate
    words unchanged. Every circuit layer has an explicit boundary, including
    layers acting on different targets. Preparation/readout remain outside the
    Spectrum block. The native components are timing fixtures, not physical MOT
    preparation or calibrated detection. A supplied NativeProgram implements
    prepare_MOT(), GM_cooling(duration), state_prep(duration), readout(duration).
    """
    if interleaved is None:
        interleaved = num_qubits == 2
    lengths = tuple(lengths)
    sequences = generate_rb_sequences(
        lengths,
        sequence_count=sequence_count,
        seed=seed,
        num_qubits=num_qubits,
        interleaved=interleaved,
    )
    qubits = example_qubits(num_qubits) if qubits is None else tuple(qubits)
    if len(qubits) != num_qubits:
        raise ValueError("The supplied qubit register does not match num_qubits")
    demo = operations is None
    ops = example_operations(qubits) if demo else operations
    native = build_rb_native_program() if native_program is None else native_program
    descriptors = {(s.length, s.sequence, s.variant): s for s in sequences}
    variants = (0, 1) if interleaved else (0,)
    exp = Experiment(
        f"rb_{num_qubits}q",
        qubits,
        metadata={
            "experiment_type": "rb",
            "num_qubits": num_qubits,
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "cz_implementation": "demonstration"
            if demo
            else "laboratory_supplied_unverified",
            "physical_cz_verified": False,
            "catalog_version": CATALOG_VERSION,
            "seed": seed,
            "variant_labels": VARIANTS,
            "rb_sequences": [s.to_dict() for s in sequences],
            "sampling": "uniform full Clifford group; per-length/sequence seeds",
            "length_definition": "random Cliffords excluding recovery",
            "readout": "joint zero-state population from same-shot classified bits",
            "preparation": "four replaceable native ARTIQ components",
            "native_fixture": native_program is None,
            "physical_preparation_verified": False,
            "preparation_to_gate_s": float(as_fraction(preparation_to_gate, "time")),
            "native_readout": "separate illumination TTL after camera trigger; Z basis",
        },
    )
    length_axis = SweepParameter("length", lengths, "dimensionless")
    sequence_axis = SweepParameter("sequence", range(sequence_count), "dimensionless")
    variant_axis = SweepParameter("variant", variants, "dimensionless")
    with exp.average(
        shots, mode="cyclic", scope="chunk" if chunks is not None else "global"
    ):
        with (
            exp.sweep(length_axis, chunks=chunks),
            exp.sweep(sequence_axis),
            exp.sweep(variant_axis),
            exp.realtime(),
        ):
            native.prepare_MOT()
            native.GM_cooling(20 * us)
            native.state_prep(8 * us)
            delay(preparation_to_gate)
            with exp.triggered_sequence(group="qubit_awg"):
                for q in qubits:
                    reset_frame(q.frame)
                for length in lengths:
                    with exp.case(length_axis, length):
                        for sequence in range(sequence_count):
                            with exp.case(sequence_axis, sequence):
                                for variant in variants:
                                    with exp.case(variant_axis, variant):
                                        descriptor = descriptors[
                                            (length, sequence, VARIANTS[variant])
                                        ]
                                        rb_sequence_section(qubits, descriptor, ops)
            acquire(
                "camera.trigger",
                duration=64 * ns,
                handle="bits",
                qubits=qubits,
                metadata={"classification_required": True, "joint_readout": True},
            )
            native.readout(10 * us)
    return exp


def synthetic_results(compiled, *, seed=2026, alpha=0.96, cz_ratio=0.98):
    """Artificial classified bits from a depolarizing curve, not pulse simulation.

    This does not predict the demo waveforms' physical behavior or replace a
    measurement. The independent RNG cannot change the frozen RB circuits.
    """
    result = ResultSet.from_compiled(
        compiled,
        metadata={
            "data_source": "synthetic_depolarizing_model_not_hardware",
            "synthetic_model": {"alpha": alpha, "cz_ratio": cz_ratio, "seed": seed},
        },
    )
    for point in compiled.points:
        for shot in range(compiled.shots):
            observation = synthetic_observation(
                point.coordinates,
                shot,
                num_qubits=len(compiled.qubits),
                seed=seed,
                alpha=alpha,
                cz_ratio=cz_ratio,
            )
            for index, q in enumerate(compiled.qubits):
                result.record("bits", point.index, shot, q, observation["bits"][index])
    return result


def synthetic_observation(
    coordinates, shot, *, num_qubits, seed=2026, alpha=0.96, cz_ratio=0.98
):
    """One reproducible artificial joint outcome, independent of chunk layout."""
    length = int(coordinates["length"])
    sequence = int(coordinates["sequence"])
    variant = int(coordinates["variant"])
    rng = np.random.default_rng(
        np.random.SeedSequence([seed, num_qubits, length, sequence, variant, shot])
    )
    d = 2**num_qubits
    decay = alpha * (cz_ratio if VARIANTS[variant] == "interleaved" else 1)
    p_zero = (1 - 1 / d) * decay**length + 1 / d
    probabilities = [p_zero] + [(1 - p_zero) / (d - 1)] * (d - 1)
    outcome = int(rng.choice(d, p=probabilities))
    return {
        "data_source": "synthetic_depolarizing_model_not_hardware",
        "coordinates": dict(coordinates),
        "shot": shot,
        "bits": [(outcome >> (num_qubits - 1 - q)) & 1 for q in range(num_qubits)],
    }


def export_synthetic_analysis(artifact, destination, *, seed):
    """Process one streamed chunk at a time; retain only compact joint counts."""
    if hasattr(artifact, "load_chunk"):
        chunks = (
            (item["index"], artifact.load_chunk(item["index"]))
            for item in artifact.manifest["chunks"]
        )
    else:
        chunks = ((None, artifact),)
    counts = []
    for index, compiled in chunks:
        results = synthetic_results(compiled)
        target = destination / "synthetic_results"
        results.save(target if index is None else target / f"chunk_{index}")
        counts.extend(
            joint_rb_counts(
                results, "bits", qubits=tuple(compiled.qubits), variant_labels=VARIANTS
            )
        )
        del results, compiled
    analysis = analyze_rb(counts, bootstrap_samples=100, seed=seed)
    analysis["data_source"] = "synthetic_depolarizing_model_not_hardware"
    (destination / "synthetic_analysis.json").write_text(
        json.dumps(analysis, indent=2, allow_nan=False), encoding="utf-8"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qubits", type=int, choices=(1, 2), default=2)
    parser.add_argument("--shots", type=int, default=128)
    parser.add_argument("--sequences", type=int, default=3)
    parser.add_argument("--seed", type=int, default=101)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Compile to a bounded managed store and export a paged viewer",
    )
    parser.add_argument(
        "--chunks", default="2", help="Positive number of length chunks, or auto"
    )
    parser.add_argument("--host-memory-mib", type=int, default=256)
    parser.add_argument(
        "--mock-run-store",
        action="store_true",
        help="Journal mock card dispatches and synthetic bits; never run kernels",
    )
    parser.add_argument(
        "--synthetic-results",
        action="store_true",
        help="also fit artificial depolarizing observations; no hardware data",
    )
    args = parser.parse_args(argv)
    if args.mock_run_store and not args.stream:
        parser.error("--mock-run-store requires --stream")
    chunks = "auto" if args.chunks == "auto" else int(args.chunks)
    suffix = "_stream" if args.stream else ""
    destination = args.output or Path(
        f"output/artiq_spectrum/rb_{args.qubits}q{suffix}"
    )
    experiment = build_experiment(
        num_qubits=args.qubits,
        shots=args.shots,
        sequence_count=args.sequences,
        seed=args.seed,
        chunks=chunks,
    )
    lab = build_lab(args.qubits)
    if args.stream:
        compiled = lab.compile_stream(
            experiment,
            directory=destination / "compiled_store",
            host_memory_budget_bytes=args.host_memory_mib * 1024**2,
        )
        report = compiled.export_report(destination / "viewer", waveform_mode="paged")
    else:
        compiled = lab.compile(experiment)
        report = compiled.export_report(destination, source_path=__file__)
    if args.synthetic_results:
        export_synthetic_analysis(compiled, destination, seed=args.seed)
    if args.mock_run_store:
        try:
            from examples.artiq_spectrum.rb_offline_run import run_offline_journal
        except ModuleNotFoundError as error:
            if error.name != "examples":
                raise
            from rb_offline_run import run_offline_journal
        run_offline_journal(
            compiled,
            destination / "mock_run",
            lambda coordinates, shot: synthetic_observation(
                coordinates, shot, num_qubits=args.qubits
            ),
        )
    print(f"Offline RB report: {report['index'].resolve()}")
    return report


if __name__ == "__main__":
    main()
