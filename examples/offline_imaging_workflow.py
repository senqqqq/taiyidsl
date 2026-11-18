"""Flat-top sweep, shared camera images and a self-contained result archive.

All frames are artificial pipeline test data, not a dynamics simulation or real
measurements. Camera illumination and atom loading are outside this example.
Run: uv run --locked python examples/offline_imaging_workflow.py
"""

from dataclasses import replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from taiyidsl import (
    Experiment,
    FlatTopGaussian,
    ImageResults,
    OpticsConfig,
    Qubit,
    SweepParameter,
    acquire,
    delay,
    play,
    reference_lab,
    us,
)


def build_scan(*, shots=12):
    qubits = [Qubit("q0"), Qubit("q1")]
    optics = OpticsConfig.from_dict(
        {
            "schema_version": 2,
            "pump": {
                "signal": "q0.raman",
                "frequency_hz": 80e6,
                "amplitude": 0.5,
                "duration_s": 10e-6,
                "setup_wait_s": 2e-6,
                "settle_s": 2e-6,
            },
        }
    )
    exp = Experiment(
        "flat_top_camera_pipeline",
        qubits,
        parameters=optics,
        metadata={
            "description": "Offline demonstration of pulse scanning and image analysis",
            "synthetic": True,
            "scope": "data pipeline only; no physical dynamics simulation",
            "readout": "occupancy; no push-out or spin inference",
        },
    )
    flat = SweepParameter("flat_duration", [0, 4, 8, 12], unit="us")
    pulse = FlatTopGaussian(flat, sigma=0.5 * us, cutoff_sigma=4)
    with exp.average(shots):
        with exp.sweep(flat):
            with exp.realtime():
                acquire(
                    "q0.readout",
                    duration=10 * us,
                    handle="pre",
                    qubits=qubits,
                    metadata={"detector": "camera0", "exposure_s": 100e-6},
                )
                # Trigger width does not set exposure or wait for data transfer.
                delay("q0.readout", 100 * us)
                play(
                    qubits[0].raman,
                    pulse,
                    amplitude=exp.parameters["pump"]["amplitude"],
                    frequency=exp.parameters["pump"]["frequency_hz"],
                    phase=0.2,
                )
                acquire(
                    "q0.readout",
                    duration=10 * us,
                    handle="post",
                    qubits=qubits,
                    metadata={"detector": "camera0", "exposure_s": 100e-6},
                )
                delay("q0.readout", 100 * us)
    lab = reference_lab(qubits)
    # Illustrative offline timing contract, NOT a measured board delay.
    # A real RAM path needs its own IO_UPDATE-to-envelope timing calibration.
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(
        lab.setup.devices[name],
        ram_latency_mu=112,
        ram_pow_latency_mu=80,
        ram_rearm_duration_mu=4000,
        ram_phase_update_duration_mu=480,
    )  # Explicit offline assumptions; not a laboratory calibration.
    return lab.compile(exp)


def make_test_images(compiled):
    images = ImageResults.from_compiled(
        compiled,
        acquisitions={
            "pre": {"camera": "camera0", "exposure_s": 100e-6},
            "post": {"camera": "camera0", "exposure_s": 100e-6},
        },
        rois={"q0": (1, 3, 1, 3), "q1": (1, 3, 5, 7)},
        thresholds={"q0": 100, "q1": 100},
        background=10,
        metadata={"synthetic": True, "description": "Artificial occupancy test frames"},
    )
    for point, shot in compiled.execution_order():
        for handle in ("pre", "post"):
            if handle == "post" and shot == 0:
                images.record_frame(handle, point, shot, status="missing")
                continue
            frame = np.full((4, 8), 10, dtype=np.uint16)
            for site, roi in images.rois.items():
                loaded = (shot + (site == "q1")) % 5 != 0
                present = loaded and (handle == "pre" or (shot + point) % 4 != 0)
                if present:
                    y0, y1, x0, x1 = roi
                    frame[y0:y1, x0:x1] += 50
            images.record_frame(handle, point, shot, frame)
    return images


def main(output=None):
    output = Path(
        output or Path(__file__).resolve().parents[1] / "output/offline_imaging"
    )
    output.mkdir(parents=True, exist_ok=True)
    compiled = build_scan()
    images = make_test_images(compiled)
    archive = images.save_hdf5(output / "results.h5")
    restored = ImageResults.load_hdf5(archive)
    summary = restored.survival()
    fig, ax = plt.subplots(figsize=(6, 4))
    for site in restored.sites:
        selected = summary.sel(site=site)
        ax.errorbar(
            selected.flat_duration.values * 1e6,
            selected.survival.values,
            yerr=selected.standard_error.values,
            marker="o",
            label=site,
        )
    ax.set(xlabel="Flat duration (us)", ylabel="Conditional survival", ylim=(0, 1.05))
    ax.set_title("Artificial frames: analysis pipeline demonstration")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output / "survival.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(6, 4))
    for point in compiled.points:
        ram = next(e for e in point.physical_events if e.kind == "dds_ram_start")
        samples = np.asarray(ram.params["ram_asf"]) / 0x3FFF
        time_us = np.arange(len(samples)) * ram.params["ram_sample_interval_s"] * 1e6
        ax.plot(
            time_us,
            samples,
            label=f"flat={point.coordinates['flat_duration'] * 1e6:g} us",
        )
    ax.set(xlabel="Time from envelope start (us)", ylabel="DDS amplitude (full scale)")
    ax.set_title("Compiled flat-top Gaussian samples")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output / "envelopes.png", dpi=150)
    plt.close(fig)
    compiled.visualize(output / "timeline.html")
    compiled.export_artiq(output / "artiq")
    print(f"Saved images, analysis settings and complete experiment context: {archive}")
    return output


if __name__ == "__main__":
    main()
