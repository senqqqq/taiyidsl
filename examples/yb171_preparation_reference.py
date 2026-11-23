"""Full Yb-171 stage reference and an executable post-loading control segment.

Run: uv run --locked python examples/yb171_preparation_reference.py

No instrument is contacted. The supplied lab can compile imaging, pumping and
RSC. MOT/coil/trap stages require laboratory builders and supported adapters;
the default output never claims that those stages have been implemented.
"""

from __future__ import annotations

import json
from pathlib import Path

from taiyidsl import Experiment, LabConfig, OpticsConfig, Qubit
from taiyidsl.experiments.cooling import illuminated_readout, sideband_cool

SOURCE = "https://doi.org/10.1103/PhysRevX.12.021027"
REQUIRED_BUILDERS = ("load", "enter_control", "readout")


def reference_workflow():
    """Return independent descriptive data, not executable hardware commands."""
    return {
        "schema_version": 1,
        "species": "171Yb",
        "reference": SOURCE,
        "reference_sections": ["Appendix A", "Appendix B.3", "Appendix D"],
        "implementation": "reference_with_external_stages",
        "feedback_rearrangement": False,
        "loading_retry": False,
        "stages": [
            {
                "name": "blue_mot",
                "purpose": "Capture atoms with 399 nm light in the science cell",
                "builder": "load",
                "requires": ["atom_source", "mot_beams", "magnetic_gradient"],
            },
            {
                "name": "green_mot",
                "purpose": "556 nm broad-line, narrow-line and compression stages",
                "builder": "load",
                "requires": ["frequency_modulation", "intensity_ramp", "coil_ramp"],
            },
            {
                "name": "tweezer_loading",
                "purpose": "Capture in tweezers, clear reservoir, reduce occupancy",
                "builder": "load",
                "requires": ["tweezer_array", "bias_field", "loading_light"],
            },
            {
                "name": "intrap_cooling",
                "purpose": "Cool loaded atoms and establish initial imaging conditions",
                "builder": "load",
                "requires": ["cooling_light", "field_control", "trap_control"],
            },
            {
                "name": "pre_image",
                "purpose": "Record initial occupancy without spin-selective removal",
                "builder": "illuminated_readout",
                "handle": "pre",
                "requires": ["imaging_light", "cooling_light", "camera"],
            },
            {
                "name": "control_conditions",
                "purpose": "Establish the bias field and trap depth for pumping/RSC",
                "builder": "enter_control",
                "requires": ["field_control", "trap_control", "settling_contract"],
            },
            {
                "name": "spin_and_motion",
                "purpose": "Initial optical pumping followed by explicit RSC rounds",
                "builder": "sideband_cool",
                "requires": ["pump_beam", "raman_beams"],
            },
            {
                "name": "readout",
                "purpose": "State mapping/removal if desired, then final fluorescence",
                "builder": "readout",
                "handle": "post",
                "requires": ["readout_conditions", "imaging_light", "camera"],
            },
        ],
        "data": {
            "pre": "Initial site occupancy; an empty site is a valid observation",
            "post": "Final occupancy; interpretation depends on readout protocol",
            "retain": ["raw_frames", "validity", "site_order", "classifier_revision"],
        },
    }


def _experiment(qubit, parameters, shots, rounds, *, stage_builders, pushout):
    full = stage_builders is not None
    exp = Experiment(
        "yb171_full_sequence" if full else "yb171_post_loading_segment",
        qubits=[qubit],
        parameters=OpticsConfig.coerce(parameters),
        metadata={
            "description": "Yb-171 preparation diagnostic with initial/final imaging",
            "reference_workflow": reference_workflow(),
            "sequence_scope": (
                "user_supplied_full_sequence" if full else "post_loading_segment"
            ),
            "external_builders_required": [] if full else list(REQUIRED_BUILDERS),
            "external_builder_validation": "not_verified",
            "physical_validation": "not_verified",
            "readout_interpretation": (
                "user_supplied"
                if full
                else "pushout_survivors"
                if pushout
                else "survival"
            ),
            "host_precondition": (
                "Camera configured; external builders supply loading and transitions"
                if full
                else "Fresh loaded/cooled atoms each shot; externally established "
                "field/trap conditions must support both imaging and pumping/RSC"
            ),
        },
    )
    q, p = exp.qubits[qubit.uid], exp.parameters
    with exp.average(shots=shots):
        with exp.realtime():
            if full:
                stage_builders["load"](exp, q, p)
            illuminated_readout(exp, q, p, pushout=False, handle="pre")
            if full:
                stage_builders["enter_control"](exp, q, p)
            # sideband_cool includes its initial pump even when rounds == 0.
            sideband_cool(exp, p, rounds=rounds)
            if full:
                stage_builders["readout"](exp, q, p, handle="post")
            else:
                illuminated_readout(exp, q, p, pushout=pushout, handle="post")
    return exp


def build_control_segment(qubit, parameters, *, shots=20, rounds=2, pushout=False):
    """Compile only existing DDS/TTL operations, with explicit host preconditions.

    The default final image measures survival. With pushout=True it measures
    survivors after state-selective removal, not a directly identified spin.
    """
    return _experiment(
        qubit, parameters, shots, rounds, stage_builders=None, pushout=pushout
    )


def build_full_sequence(qubit, parameters, *, stage_builders, shots=20, rounds=2):
    """Compose supplied physical stage builders; never fill gaps with fake TTLs.

    Required ordinary Python functions:
      load(exp, qubit, parameters): loading through initial imaging conditions.
      enter_control(exp, qubit, parameters): imaging to spin/RSC conditions.
      readout(exp, qubit, parameters, *, handle): complete final readout,
        including state mapping if desired and measure(..., handle=handle).

    Functions run once during construction inside a realtime block, not on the
    instruments. They must emit supported DSL operations with explicit timing;
    runtime camera processing, loading retries and feedback are not supported.
    Providing a builder does not certify its physical implementation.
    """
    missing = [
        name for name in REQUIRED_BUILDERS if not callable(stage_builders.get(name))
    ]
    if missing:
        raise ValueError(
            "Full preparation requires explicit stage builders: " + ", ".join(missing)
        )
    return _experiment(
        qubit, parameters, shots, rounds, stage_builders=stage_builders, pushout=False
    )


def main(output_dir=None):
    root = Path(__file__).resolve().parents[1]
    output = (
        Path(output_dir)
        if output_dir is not None
        else root / "output/yb171_preparation_reference"
    )
    output.mkdir(parents=True, exist_ok=True)
    q = Qubit("q0")
    lab = LabConfig.load(root / "examples/config/labs/rsc.json", qubit_id=q.uid)
    parameters = OpticsConfig.load(root / "examples/config/optics/rsc.json")
    experiment = build_control_segment(q, parameters)
    compiled = lab.compile(experiment)
    (output / "workflow.json").write_text(
        json.dumps(reference_workflow(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    compiled.save(output / "control_segment.json")
    compiled.visualize(output / "control_segment.html")
    print(f"Reference stages and post-loading segment saved to {output}")
    print("MOT/field/trap builders and real camera collection remain unconfigured.")


if __name__ == "__main__":
    main()
