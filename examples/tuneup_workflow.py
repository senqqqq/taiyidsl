"""Run the complete synthetic tune-up workflow without touching instruments.

Run: uv run python examples/tuneup_workflow.py
All output is generated under output/tuneup_workflow by default. This example
uses an explicitly illustrative device setup, not a laboratory device database.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from taiyidsl.artiq import verify_equivalence
from taiyidsl.lab import reference_lab
from taiyidsl.models import Qubit, QubitStore
from taiyidsl.results import synthesize
from taiyidsl.results.analysis import analyze_rabi, analyze_ramsey, analyze_rb
from taiyidsl.templates import rabi, ramsey, randomized_benchmarking


def source_metrics(path):
    """Count source lines and per-shot kernels, excluding device initialization."""
    source = Path(path).read_text(encoding="utf-8")
    kernels = [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name != "initialize_devices"
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "kernel"
            for decorator in node.decorator_list
        )
    ]
    return {
        "source_lines": len(source.splitlines()),
        "kernel_count": len(kernels),
        "kernels": kernels,
    }


def run(output_dir):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    store = QubitStore([Qubit("q0")], path=output_dir / "qubits.json")
    store.save()
    lab = reference_lab([store["q0"]])
    report = {
        "synthetic_only": True,
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
        "experiments": {},
    }

    def produce(exp, seed, model_parameters):
        # Opt in explicitly: fitted durations need quantization to target grids.
        compiled = lab.compile(exp, allow_quantization=True)
        folder = output_dir / exp.name
        folder.mkdir(parents=True, exist_ok=True)
        compiled.save(folder / "compiled.json")
        compiled.visualize(folder / "timeline.html")
        exported = compiled.export_artiq(folder / "artiq", mode="auto")
        unrolled = compiled.export_artiq(folder / "artiq_unrolled", mode="unrolled")
        comparison = {
            "auto": source_metrics(exported["source"]),
            "unrolled": source_metrics(unrolled["source"]),
            "equivalence": verify_equivalence(compiled),
        }
        (folder / "export_comparison.json").write_text(
            json.dumps(comparison, indent=2), encoding="utf-8"
        )
        results = synthesize(compiled, seed=seed, model_parameters=model_parameters)
        results.save(folder / "results")
        report["experiments"][exp.name] = {
            "points": len(compiled.points),
            "shots_per_point": compiled.shots,
            "averaging_mode": compiled.averaging_mode,
            "qubit_revision": compiled.qubits["q0"].revision,
            "export_verification": exported["verification"],
            "export_comparison": comparison,
            "quantization_diagnostics": compiled.diagnostics,
        }
        return results, folder

    rabi_exp = rabi(
        store["q0"], [i * 1.5e-6 for i in range(49)], shots=300, mode="cyclic"
    )
    rabi_data, folder = produce(rabi_exp, 10, {"pi_duration": 12e-6})
    proposal = analyze_rabi(rabi_data, experiment=rabi_exp)
    (folder / "calibration_proposal.json").write_text(
        json.dumps(proposal.to_dict(), indent=2), encoding="utf-8"
    )
    # An explicit application decision, after reviewing the generated suggestion.
    if proposal.quality["r_squared"] < 0.95:
        raise RuntimeError("Synthetic Rabi fit failed the example acceptance criterion")
    updated = store.apply(proposal, persist=True)
    report["calibration"] = {
        "old_revision": 0,
        "new_revision": updated.revision,
        "pi_duration": updated.params.raman.pi_duration,
        "original_experiment_pi_duration": rabi_exp.qubits[
            "q0"
        ].params.raman.pi_duration,
    }

    ramsey_exp = ramsey(
        store["q0"],
        [i * 3e-6 for i in range(41)],
        shots=300,
        mode="sequential",
        detuning=45e3,
    )
    ramsey_data, folder = produce(ramsey_exp, 11, {"t2star": 90e-6})
    ramsey_fit = analyze_ramsey(ramsey_data)
    (folder / "analysis.json").write_text(
        json.dumps(ramsey_fit.to_dict(), indent=2), encoding="utf-8"
    )

    rb_exp = randomized_benchmarking(
        store["q0"],
        [0, 1, 2, 4, 8, 16, 32],
        sequence_count=3,
        shots=200,
        seed=99,
        inter_gate_delay=2e-6,
        mode="cyclic",
    )
    rb_data, folder = produce(rb_exp, 12, {"p": 0.96})
    rb_fit = analyze_rb(rb_data)
    (folder / "analysis.json").write_text(
        json.dumps(rb_fit.to_dict(), indent=2), encoding="utf-8"
    )
    report["ramsey_fit"] = ramsey_fit.to_dict()
    report["rb_fit"] = rb_fit.to_dict()
    (output_dir / "verification_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "output" / "tuneup_workflow",
    )
    args = parser.parse_args()
    report = run(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "experiments": list(report["experiments"]),
                "calibration": report["calibration"],
                "artiq_compile": report["artiq_compile"],
                "hardware": report["hardware"],
            },
            indent=2,
        )
    )
