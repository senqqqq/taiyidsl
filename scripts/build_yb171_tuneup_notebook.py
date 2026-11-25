"""Build the offline, plain-DSL 171Yb tune-up laboratory notebook."""

# Long Chinese explanations and Markdown tables are intentional.
# ruff: noqa: E501
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "examples/artiq_spectrum/ramsey_lab"
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source, *, tags=()):
    cells.append(
        nbformat.v4.new_code_cell(
            textwrap.dedent(source).strip(),
            metadata={"tags": list(tags)} if tags else {},
        )
    )


md(r"""
# ¹⁷¹Yb nuclear-spin qubit tune-up with global RF and interaction-zone clock and Rydberg drives

Use $|0\rangle=|{}^1S_0,m_F=-1/2\rangle$ and $|1\rangle=|{}^1S_0,m_F=+1/2\rangle$. The selected clock transition shelves $|1\rangle$ in $|c\rangle\in{}^3P_0$, followed by a drive on $|c\rangle\leftrightarrow|r\rangle$. Verify the Zeeman branch, polarization and field in the laboratory.

Run this workbook from top to bottom offline. It includes complete configuration, named pulses, plain Python DSL experiments, scan and acquisition guidance, runnable analysis, representative sequence compilation and calibration records. Plots and fits identify synthetic or model data. Actual preparation, transport and detection require laboratory implementations.

| Order | Experiments | Calibration or diagnostic |
|---|---|---|
| 0 | Loading, cooling, field, SPAM, AOMs, transport and survival | Control chain and valid denominators |
| 1 | RF spectroscopy, 2D Rabi/Ramsey, echo, CPMG, XY8 and population hold | Resonance, π/π2, coherence and loss |
| 2 | Axis phase, ALLXY, amplitude-error amplification and global RF 1Q RB | Systematic errors and single-qubit behavior by site |
| 3 | Optical clock chevrons, Ramsey, shelving phase/hold, light shifts and optical RB | Return, coherence, crosstalk and leakage |
| 4 | Rydberg chevrons, return/hold, single occupancy and pairs | Detuning, collective Rabi response, blockade and recapture |
| 5 | CZ closure and echo, Bell/parity, input-dependent loss, randomized diagnostics, SSB and waveform candidates | Measurable entangling-gate performance with global control |
| 6 | Raw shots, uncertainty, calibration changes and daily checks | Reproducible tune-up |

Start with device databases and wiring, configure `LabConfig` and qubits, then write experiments and compile with `lab.compile(...)`. The two-qubit sequence uses echo to tune entangling phase, Bell parity to set common phase, randomized echo for diagnostics and echo-free SSB for benchmarking. See the [plain Ramsey tutorial](ramsey_clock_plain.ipynb) and [development guide](../development.md).

From the project root run `uv sync --locked --group notebooks`, then `uv run --locked --group notebooks jupyter lab docs/tutorials/yb171_global_rf_tuneup.ipynb`. Choose the project's CPython 3.12 kernel.
""")
md(r"""
## 1. Available controls

- Global RF reaches every registered atom in $^1S_0$, including storage spectators. q0 supplies the calibration for the shared waveform. Measure rotation angle, resonance and contrast per site.
- Clock and Rydberg beams illuminate all atoms in the interaction zone, without independent optical addressing within it. Moving an atom does not provide local RF control.
- Each shot here holds q0/q1 in one zone and includes an RF-only `spectator`. For dynamic occupancy, declare `IZ` and `TransportConfig` and schedule `move`. The compiler derives occupancy from positions. Editing `targets` or `position` alone cannot move an atom. See [transport Ramsey](ramsey_clock_transport.ipynb).
- Independent local X/Y operations required for full 2Q Clifford RB are unavailable. The pair protocol mainly probes the symmetric subspace. Symmetry-breaking noise can still be visible.
- One complete ground-state CZ includes clock π shelving, the Rydberg gate, clock π return, gaps and common-phase compensation. Repeat this entire sequence when counting ground-state CZ gates.

Before taking data, check polarization and field axes, zone-wide Ω and AC Stark uniformity, interatomic spacing and pair crosstalk, AOM extinction/phase jumps/acoustic delay, optical phase reference, trap heating/recapture and camera registration. Compare storage-spectator RF Ramsey with and without zone control. Shelving one spin component cannot protect an arbitrary spectator state.
""")
code(
    """
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit
from taiyidsl import (
    DeviceSetup, Experiment, Hz, LabConfig, NativeKernelPackage, Qubit,
    Square, SweepParameter, deg, delay, kHz, ms, ns, play, rad, reset_phase, us,
)
""",
    tags=("rb-imports",),
)
code("""
from IPython.display import Image, Markdown, display

ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src/taiyidsl").is_dir())
OUTPUT = ROOT / "output/yb171_tuneup"
OUTPUT.mkdir(parents=True, exist_ok=True)
RNG = np.random.default_rng(171)
SHOTS_PREVIEW = 2

""")
md(r"""
## 2. From device_db and spec_db to the descriptor, LabConfig and Qubit

On the two Spectrum cards, A0 drives RF, B0 drives the clock AOM and B1 drives the Rydberg AOM. The 1.25 GHz setting is the card sample rate. Addresses, RTIO channels, delays and amplitudes are offline placeholders. Keep the laboratory MOT device database intact.

`device_db` describes ARTIQ devices, `spec_db` configures the cards and the descriptor specifies wiring. `DeviceSetup` reads them and `LabConfig` provides the compilation and connection entry points. Register every atom exposed to global RF. For clock and Rydberg, `sharing="global"` means that the zone members share a physical output. The illuminated area is defined by the apparatus.
""")
code((LAB / "device_db.py").read_text(encoding="utf-8"))
code((LAB / "spec_db.py").read_text(encoding="utf-8"))
code("""
descriptor = {
    "name": "yb171_zone_tuneup", "backend": "artiq-spectrum",
    "instruments": {
        "core": {"kind": "artiq", "device": "core"},
        "A": {"kind": "spectrum", "device": "A"},
        "B": {"kind": "spectrum", "device": "B"},
    },
    "channels": {
        "nuclear_rf": {"instrument": "A", "channel": 0, "sharing": "global"},
        "clock_zone": {"instrument": "B", "channel": 0, "sharing": "global"},
        "rydberg_zone": {"instrument": "B", "channel": 1, "sharing": "global"},
        "camera": {"instrument": "core", "device": "ttl_Camera_Trig"},
        "awg_trigger_a": {"instrument": "core", "device": "awg_trigger_a"},
        "awg_trigger_b": {"instrument": "core", "device": "awg_trigger_b"},
    },
    "sync_groups": {"qubit_awg": {
        "cards": ["A", "B"],
        "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b"},
    }},
    "signals": {
        "q0": {"rf": "nuclear_rf", "clock": "clock_zone", "rydberg": "rydberg_zone"},
        "q1": {"rf": "nuclear_rf", "clock": "clock_zone", "rydberg": "rydberg_zone"},
        "spectator": {"rf": "nuclear_rf"},
        "camera": "camera",
    },
}
setup = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
display(setup.connections())
""")
md(r"""
### 2.1 Qubit roles, calibrations and lab configuration

q0, q1 and the spectator each take a private snapshot of the example parameters. Assign explicit roles in `signals`. The descriptor determines which roles share an output.
""")
code("""
parameters = {
    "rf": {"frequency": 30*kHz, "pi_duration": 200*us, "pi2_duration": 100*us,
           "pi_amplitude": .2, "pi2_amplitude": .2, "waveform": "square", "frame": "spin"},
    "clock": {"frequency": 200*kHz, "pi_duration": 100*us, "pi2_duration": 50*us,
              "pi_amplitude": .2, "pi2_amplitude": .2, "waveform": "square", "frame": "clock_control"},
    "rydberg": {"frequency": 500*kHz, "pi_duration": 2*us, "pi2_duration": 1*us,
                "pi_amplitude": .2, "pi2_amplitude": .2, "waveform": "square", "frame": "rydberg_control"},
    "extensions": {"calibration": {"status": "illustrative_uncalibrated"}},
}
q0 = Qubit(uid="q0", signals={"rf": "q0.rf", "clock": "q0.clock", "rydberg": "q0.rydberg"}, parameters=parameters)
q1 = Qubit(uid="q1", signals={"rf": "q1.rf", "clock": "q1.clock", "rydberg": "q1.rydberg"}, parameters=parameters)
spectator = Qubit(uid="spectator", signals={"rf": "spectator.rf"}, parameters={"rf": parameters["rf"]})
lab = LabConfig(setup=setup, qubits=[q0, q1, spectator], emulation=True)
""")
md(r"""
### 2.2 Measure the AOM frequency and phase mapping

For each optical path, define $\delta_{\rm opt}=s_f(f_{\rm AWG}-f_{\rm AWG,0})$ and $\phi_{\rm opt}=s_\phi\phi_{\rm AWG}+\phi_0$. Diffraction order and double pass can change the sign or scale of $s_f$. Measure $s_\phi$ using interference or atomic Ramsey. The example uses +1.

`frequency` stores the electrical carrier. The 200 and 500 kHz intermediates simplify offline inspection. Convert desired optical detunings to the final electrical frequencies before building the sweep. `SweepParameter` does not support arbitrary addition or division.
""")
code("""
optical_mapping = {"clock": {"frequency_scale": 1., "phase_scale": 1.},
                   "rydberg": {"frequency_scale": 1., "phase_scale": 1.}}

def optical_frequency_axis(q, role, detunings_hz):
    base = getattr(q.parameters, role).frequency
    scale = optical_mapping[role]["frequency_scale"]
    return SweepParameter(role + "_drive_hz", [base + float(d)/scale for d in detunings_hz], "Hz")

# Pulses store amplitude and duration. Frequency defaults to the experiment snapshot.
rf_pi = Square(duration=q0.parameters.rf.pi_duration, amplitude=q0.parameters.rf.pi_amplitude)
rf_pi2 = Square(duration=q0.parameters.rf.pi2_duration, amplitude=q0.parameters.rf.pi2_amplitude)
clock_pi = Square(duration=q0.parameters.clock.pi_duration, amplitude=q0.parameters.clock.pi_amplitude)
clock_pi2 = Square(duration=q0.parameters.clock.pi2_duration, amplitude=q0.parameters.clock.pi2_amplitude)
rydberg_pi = Square(duration=q0.parameters.rydberg.pi_duration, amplitude=q0.parameters.rydberg.pi_amplitude)
""")
md(r"""
### 2.3 Native ARTIQ preparation and detection

The GreenMOT TTL fixture illustrates interfaces. Laboratory preparation must load, cool and optically pump the atoms to 00, select or transport the zone members, switch traps and allow measured settling time. Readout needs site-resolved 0, 1, residual clock population, loss and invalid classifications.

Place native callbacks at preparation/readout boundaries in `realtime`. Native operations with unspecified duration cannot run inside the AWG sequence. Configured DDS transport uses `move`. Give the camera trigger to native readout alone. Compilation produces no camera frames. See [readout adapters](../readout_adapters.md).

The fixture uses `NativeKernelPackage.from_package` with `core.reset()` in its legacy initialization and cleanup after each shot. New independent MOT, cooling and camera components can use `NativeModule.from_package` with `NativeProgram`. The session then owns reset and component `finalize` runs at session end. See [native components](../native_components.md) and the [laboratory extension example](../../examples/lab_extension/README.md).
""")
code("""
package = NativeKernelPackage.from_package(
    root=ROOT / "examples/artiq_spectrum/greenmot_native",
    entry="greenmot_fixture:GreenMOTFixture",
    resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
    initialize_method="initialize_native", finalize_method="finalize_native",
)
prepare_prefix = package.kernel("prepare_MOT", timing="prefix")
prepare_tail = package.kernel("finish_preparation", timing="timed", duration=15*ms+16*ns,
                              end_boundary="OFFLINE fixture TTL boundary; no atoms or transport")
readout_fixture = package.kernel("execute_readout", timing="suffix", duration=100.48*ms)

def prepare_fixture():
    prepare_prefix()
    prepare_tail()

callbacks = {"prepare": prepare_fixture, "readout": readout_fixture, "shots": SHOTS_PREVIEW}

""")
md(r"""
## 3. Establish SPAM, loss and leakage denominators

Distinguish a site empty before loading, loss after loading, invalid detection, a surviving atom outside the logical states, and valid logical 0/1. A dark site alone cannot establish logical 1. If clock leakage is unobservable per shot, estimate it with separate repump/return controls and label those as different shots.

Acquire prepared 0, prepared 1, no-pushout, empty, clock-shelved and repump on/off controls. Estimate per-site confusion matrices with observed rows and true columns and check correlated pair errors. Report raw counts first. Use a constrained likelihood for correction since direct inversion may yield negative probabilities. Gate and SPAM estimates need independent calibration.

The synthetic counts below demonstrate denominators. Estimate joint $P_{00}$ from the two atoms in each shot. Multiplying the site marginals discards correlations.
""")
code("""
def summarize_pair_counts(counts):
    required = {"00", "01", "10", "11", "leak", "loss", "invalid"}
    if set(counts) != required or any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError("Use exclusive, nonnegative integer outcome counts")
    total = sum(counts.values())
    valid = total - counts["invalid"]
    logical = sum(counts[k] for k in ("00", "01", "10", "11"))
    if total == 0 or valid == 0:
        raise ValueError("No valid acquired trials")
    return {"attempts": total, "valid_trials": valid,
            "invalid_fraction": counts["invalid"]/total,
            "atom_survival": (valid-counts["loss"])/valid,
            "logical_survival": logical/valid,
            "P00_unconditional": counts["00"]/valid,
            "P00_given_logical": counts["00"]/logical if logical else None,
            "leakage_fraction": counts["leak"]/valid}

synthetic_counts = {"00": 850, "01": 20, "10": 15, "11": 30, "leak": 25, "loss": 50, "invalid": 10}
display([summarize_pair_counts(synthetic_counts)])
assert summarize_pair_counts({"00":0,"01":0,"10":0,"11":0,"leak":1,"loss":1,"invalid":0})["P00_given_logical"] is None
""")
md(r"""
## 4. RF qubit spectroscopy

Prepare 0, fix a weak, long probe and scan final RF frequency. Use a coarse scan followed by steps of roughly one fifth of the linewidth or smaller. For strong drive, fit a driven two-level response. Inspect resonance and linewidth by site for field gradients. Choose a shared frequency using the whole distribution, including the worst site. Vary field and trap depth to separate Zeeman and light shifts.
""")
code("""
rf_frequency = SweepParameter("rf_frequency", [28000, 30000, 32000], "Hz")
rf_probe = Square(duration=400*us, amplitude=.05, frequency=rf_frequency)

def rf_spectroscopy(q, frequency_axis, probe, *, prepare, readout, shots=2):
    exp = Experiment("rf_spectroscopy", [q], metadata={"data": "not_acquired"})
    with exp.average(shots), exp.sweep(frequency_axis), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, probe)
        readout()
    return exp

rf_spec_exp = rf_spectroscopy(q0, rf_frequency, rf_probe, **callbacks)
rf_spec_preview = lab.compile(rf_spec_exp)
display(rf_spec_preview.axes)
""")
md(r"""
## 5. RF duration Rabi and π/π2 calibration

Fix carrier and power. Sweep at least two or three periods before fine scans around the π and π/2 pulses. Fit offset, contrast, decay and detuning as appropriate. Acquire an independent no-pulse background for zero duration. Store `pi2_duration/pi2_amplitude` and `pi_duration/pi_amplitude` separately. The half-duration relation assumes constant Ω. Inspect RF switch and amplifier turn-on and ringing.
""")
code("""
rf_lengths = SweepParameter("rf_length", [50, 100, 200, 300], "us")
rf_rabi_pulse = Square(duration=rf_lengths, amplitude=q0.parameters.rf.pi_amplitude)

def rf_rabi(q, lengths, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_length_rabi", [q])
    with exp.average(shots), exp.sweep(lengths), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pulse)
        readout()
    return exp

rf_rabi_preview = lab.compile(rf_rabi(q0, rf_lengths, rf_rabi_pulse, **callbacks))

def damped_rabi(t, offset, amplitude, omega, decay):
    return offset - amplitude*np.exp(-t/decay)*np.cos(omega*t)

# SYNTHETIC data only; no automatic calibration update.
times = np.linspace(0, 1000e-6, 81)
p_true = damped_rabi(times, .5, .47, 2*np.pi*2500, .004)
p_demo = RNG.binomial(800, p_true)/800
fit_rabi, _ = curve_fit(damped_rabi, times, p_demo, p0=[.5,.45,2*np.pi*2400,.004],
                       bounds=([.3,0,2*np.pi*1000,1e-5],[.7,.5,2*np.pi*5000,.1]))
fig, ax = plt.subplots(figsize=(7,3))
ax.plot(times*1e6, p_demo, ".", label="Synthetic shots")
ax.plot(times*1e6, damped_rabi(times,*fit_rabi), label="Fit")
ax.set(xlabel="RF pulse duration (us)", ylabel="P1", title="SYNTHETIC RF Rabi")
ax.legend()
fig.savefig(OUTPUT / "synthetic_rf_rabi.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "synthetic_rf_rabi.png")))
print("Synthetic pi duration (us):", np.pi/fit_rabi[2]*1e6)
""")
md(r"""
### 5.1 Two-dimensional Rabi scans

Scan frequency × duration to locate resonance and π time. At fixed frequency, scan amplitude × duration to measure how the DAC setting maps to Rabi rate. The offline example compiles 3×3 points. The outer axis indexes rows and the inner axis changes fastest across columns. Aggregate real shots by recorded coordinates and leave missing frames empty. Convert detunings to positive final electrical frequencies for `frequency=`. `detuning=` does not accept a scan axis. Each point has preparation/readout around a triggered sequence containing just its physical pulses.
""")
code("""
rf_chevron_frequency = SweepParameter("rf_chevron_hz", [q0.parameters.rf.frequency+d for d in (-1000,0,1000)], "Hz")
rf_chevron_time = SweepParameter("rf_chevron_time", [100,200,300], "us")
rf_amplitudes = SweepParameter("rf_amplitude", [.16,.20,.24], "dimensionless")
rf_chevron_pulse = Square(duration=rf_chevron_time, amplitude=q0.parameters.rf.pi_amplitude)
rf_amplitude_pulse = Square(duration=rf_chevron_time, amplitude=rf_amplitudes)

def rf_chevron(q, frequency, duration, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_chevron_2d", [q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pulse, frequency=frequency)
        readout()
    return exp

def rf_amplitude_rabi(q, amplitude, duration, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_amplitude_rabi_2d", [q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pulse)
        readout()
    return exp

rf_chevron_preview = lab.compile(rf_chevron(q0,rf_chevron_frequency,rf_chevron_time,rf_chevron_pulse,**callbacks))
rf_amplitude_preview = lab.compile(rf_amplitude_rabi(q0,rf_amplitudes,rf_chevron_time,rf_amplitude_pulse,**callbacks))
""")
md(r"""
## 6. Ramsey, echo, CPMG and frequency locking

Sweep the Ramsey dark time with `frequency=rf.frequency+detuning`. The shared carrier already evolves over the dark interval. Adding $2\pi\delta\tau$ to the second pulse would count it twice. Reset the phase reference once at sequence entry. This changes the drive reference independently of the atomic state.

Use intentional detuning to resolve several fringes, then collect ±90° analysis quadratures. Interleaving two frequency probes can feed a host-side lock. Require enough valid shots and contrast, limit the update step, and rebuild after updating calibration. The example does not implement live feedback.

Hahn echo scans `half_tau`, with total dark time twice that value. Change π phase to expose pulse errors. CPMG uses Y π rotations, τ/2 at both ends and τ between rotations. Record dark time and elapsed time including pulses. Real T2 estimates require data spanning the decay.
""")
code("""
def rf_ramsey(q, tau, pi2_pulse, *, detuning=1*kHz, phase=0*deg, prepare, readout, shots=2):
    frequency = q.parameters.rf.frequency*Hz + detuning
    exp = Experiment("rf_ramsey", [q])
    with exp.average(shots), exp.sweep(tau), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pi2_pulse, frequency=frequency)
            delay(q.rf, tau)
            play(q.rf, pi2_pulse, frequency=frequency, phase=phase)
        readout()
    return exp

def rf_echo(q, half_tau, pi2_pulse, pi_pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_hahn_echo", [q])
    with exp.average(shots), exp.sweep(half_tau), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pi2_pulse)
            delay(q.rf, half_tau)
            play(q.rf, pi_pulse, phase=90*deg)
            delay(q.rf, half_tau)
            play(q.rf, pi2_pulse, phase=180*deg)
        readout()
    return exp

def rf_cpmg(q, pi2_pulse, pi_pulse, *, tau, echoes, prepare, readout, shots=2):
    if type(echoes) is not int or echoes < 1:
        raise ValueError("echoes must be a positive integer")
    exp = Experiment("rf_cpmg", [q], metadata={"echoes": echoes, "dark_time_s": float(tau)*echoes})
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pi2_pulse)
            delay(q.rf, tau/2)
            for i in range(echoes):
                play(q.rf, pi_pulse, phase=90*deg)
                delay(q.rf, tau/2 if i == echoes-1 else tau)
            play(q.rf, pi2_pulse, phase=180*deg)
        readout()
    return exp

tau = SweepParameter("tau", [10,100,1000], "us")
half_tau = SweepParameter("half_tau", [10,100,500], "us")
ramsey_preview = lab.compile(rf_ramsey(q0,tau,rf_pi2,**callbacks))
ramsey_preview.export_report(OUTPUT / "rf_ramsey_report")
echo_preview = lab.compile(rf_echo(q0,half_tau,rf_pi2,rf_pi,**callbacks))
cpmg_preview = lab.compile(rf_cpmg(q0,rf_pi2,rf_pi,tau=100*us,echoes=2,**callbacks))
""")
md(r"""
### 6.1 Ramsey detuning or analysis phase versus dark time

Use the frequency scan for resonance and drift. At each dark time, fit offset, contrast and phase over the analysis-phase scan to distinguish drift from contrast decay. Both half π pulses share a frequency; reset once at the beginning. The model includes finite detuned rotations. For the phase scan, hold RF frequency fixed and vary only the final pulse phase. Dark time is measured between pulse edges.
""")
code("""
rf_ramsey_frequency = SweepParameter("rf_ramsey_hz", [q0.parameters.rf.frequency+d for d in (-1000,0,1000)], "Hz")
rf_dark_times = SweepParameter("rf_dark_time", [10,100,500], "us")
rf_analysis_phases = SweepParameter("rf_analysis_phase", [0,90,180,270], "deg")

def rf_ramsey_map(q, frequency, dark_time, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_ramsey_frequency_time", [q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(dark_time), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,pulse,frequency=frequency)
            delay(q.rf,dark_time)
            play(q.rf,pulse,frequency=frequency)
        readout()
    return exp

def rf_ramsey_phase_map(q, phase, dark_time, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_ramsey_phase_time", [q])
    with exp.average(shots), exp.sweep(phase), exp.sweep(dark_time), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,pulse)
            delay(q.rf,dark_time)
            play(q.rf,pulse,phase=phase)
        readout()
    return exp

rf_ramsey_map_preview = lab.compile(rf_ramsey_map(q0,rf_ramsey_frequency,rf_dark_times,rf_pi2,**callbacks))
rf_ramsey_phase_preview = lab.compile(rf_ramsey_phase_map(q0,rf_analysis_phases,rf_dark_times,rf_pi2,**callbacks))
""")
md(r"""
### 6.2 XY8 and population hold

XY8 applies X Y X Y Y X Y X π axes, with `half_gap` at either end and twice that interval between pulses. For N π pulses the dark time is 2N·half_gap. Total time includes the π pulses and the two π/2 rotations. Compare orthogonal initial coherences against Hahn and CPMG. Here x/y label the first π/2 axis, preparing −Y/+X from +Z. Protection depends on the initial state and pulse errors. [Tyryshkin et al.](https://arxiv.org/abs/1011.1903)

For population hold, prepare 0 and 1 in equal-length preparation slots and record P0/P1/loss/invalid per site. Both input states constrain equilibrium population and transition rates. Measure survival separately from spin T1. Extend the short preview window to the actual coherence and drift scales.
""")
code("""
xy8_half_gaps = SweepParameter("xy8_half_gap", [10,50,100], "us")
population_holds = SweepParameter("population_hold", [10,100,1000], "us")

def rf_xy8(q, half_gap, half_pulse, pi_pulse, *, blocks=1, initial_phase=0*deg, prepare, readout, shots=2):
    if type(blocks) is not int or blocks < 1:
        raise ValueError("blocks must be a positive integer")
    phases = [0,90,0,90,90,0,90,0]*blocks
    exp = Experiment("rf_xy8", [q], metadata={"pi_count":len(phases),"initial_phase_rad":float(initial_phase)})
    with exp.average(shots), exp.sweep(half_gap), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,half_pulse,phase=initial_phase)
            delay(q.rf,half_gap)
            for i,phase in enumerate(phases):
                play(q.rf,pi_pulse,phase=phase*deg)
                delay(q.rf,half_gap)
                if i+1 < len(phases):
                    delay(q.rf,half_gap)
            play(q.rf,half_pulse,phase=initial_phase+180*deg)
        readout()
    return exp

def rf_population_hold(q, hold, pi_pulse, *, prepare_one, prepare, readout, shots=2):
    exp = Experiment(f"rf_population_hold_{int(prepare_one)}", [q])
    with exp.average(shots), exp.sweep(hold), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            if prepare_one:
                play(q.rf,pi_pulse)
            else:
                delay(q.rf,pi_pulse.duration)
            delay(q.rf,hold)
        readout()
    return exp

xy8_x_preview = lab.compile(rf_xy8(q0,xy8_half_gaps,rf_pi2,rf_pi,**callbacks))
xy8_y_preview = lab.compile(rf_xy8(q0,xy8_half_gaps,rf_pi2,rf_pi,initial_phase=90*deg,**callbacks))
population_zero_preview = lab.compile(rf_population_hold(q0,population_holds,rf_pi,prepare_one=False,**callbacks))
population_one_preview = lab.compile(rf_population_hold(q0,population_holds,rf_pi,prepare_one=True,**callbacks))
""")
md(r"""
### 6.3 Spin lock versus amplitude and duration

Yπ/2 prepares +X. A continuous X drive locks the spin and −Yπ/2 maps it back for readout. Set the drive on resonance and keep the reference half pulses fixed as you change lock amplitude. Compare rotating-frame retention and loss at each setting. Extract T1ρ only after controlling detuning, pulse errors and readout drift. Noise-spectrum reconstruction additionally needs a response function, filter bandwidth and noise model.
""")
code("""
lock_amplitudes = SweepParameter("lock_amplitude", [.08,.12,.16], "dimensionless")
lock_durations = SweepParameter("lock_duration", [100,500,1000], "us")
rf_lock_pulse = Square(duration=lock_durations,amplitude=lock_amplitudes)

def rf_spin_lock(q, amplitude, duration, half_pulse, lock_pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_spin_lock_2d",[q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,half_pulse,phase=90*deg)
            play(q.rf,lock_pulse)
            play(q.rf,half_pulse,phase=270*deg)
        readout()
    return exp

spin_lock_preview = lab.compile(rf_spin_lock(q0,lock_amplitudes,lock_durations,rf_pi2,rf_lock_pulse,**callbacks))
""")
md(r"""
## 7. RF axis phase, ALLXY and error amplification

Start with Xπ/2–Rφπ/2 scans to check 90° X/Y separation and 180° negative axes. Exchange axes and compare Ramsey to help separate detuning and phase offset.

ALLXY uses 21 pairs. Lowercase x/y denote half π, uppercase X/Y denote π and I idles for the half π duration. Ideal P1 is 0 for the first five pairs, 1/2 for the next twelve and 1 for the last four. Compare all sites under the same broadcast sequence to diagnose detuning, pulse area and axis errors.

Scan repetition n and amplitude offset for $(X_\pi)^{2n}$, $(X_{\pi/2})^{4n}$ and alternating positive/negative axes. Check drift and leakage before picking an extremum in long sequences.
""")
code("""
# Load numerical helpers. The experiment cells below show the full control sequence.
import importlib.util

spec = importlib.util.spec_from_file_location("yb_analysis", ROOT / "examples/yb171_tuneup_analysis.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)
""")
code("""
rf_gate_map = {"I": (None,0), "x":(rf_pi2,0), "y":(rf_pi2,90), "X":(rf_pi,0), "Y":(rf_pi,90),
               "x90":(rf_pi2,0), "xm90":(rf_pi2,180), "y90":(rf_pi2,90), "ym90":(rf_pi2,270),
               "x180":(rf_pi,0), "y180":(rf_pi,90)}

def rf_phase_scan(q, phase, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_axis_phase", [q])
    with exp.average(shots), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,pulse)
            play(q.rf,pulse,phase=phase)
        readout()
    return exp

def rf_word_experiment(q, word, gate_map, *, name, prepare, readout, shots=2):
    exp = Experiment(name,[q],metadata={"word":list(word),"control":"same RF on every registered atom"})
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            for gate in word:
                pulse, phase = gate_map[gate]
                if pulse is None:
                    delay(q.rf, q.parameters.rf.pi2_duration)
                else:
                    play(q.rf,pulse,phase=phase*deg)
        readout()
    return exp

axis_phases = SweepParameter("rf_axis_phase", [0,90,180,270], "deg")
axis_preview = lab.compile(rf_phase_scan(q0,axis_phases,rf_pi2,**callbacks))
allxy_experiments = [rf_word_experiment(q0,word,rf_gate_map,name=f"allxy_{i:02}",**callbacks)
                     for i,word in enumerate(analysis.ALLXY_PAIRS)]
allxy_preview = lab.compile(allxy_experiments[6])
amplification_preview = lab.compile(rf_word_experiment(q0,["x"]*8,rf_gate_map,name="rf_area_amplification",**callbacks))
display([{"index":i,"pulses":" → ".join(pair),"ideal_P1":round(p,5)}
           for i,(pair,p) in enumerate(zip(analysis.ALLXY_PAIRS,analysis.allxy_expected()))])
""")
md(r"""
### 7.1 Reading amplitude-error and two-dimensional model plots

After a fixed reference half π, ideal pairs of Xπ leave P1=1/2. Small area errors grow with repetition. For the half-π calibration use four trial half pulses per group, with N=0 as a reference. Change only the trial amplitude. Check detuning, ALLXY and loss alongside the amplified error.

Both figures are noiseless isolated two-level models. The Ramsey plot includes finite detuned half pulses. Measured populations require classified shots, and the Spectrum output alone cannot predict them.
""")
code("""
area_blocks = SweepParameter("area_blocks", [0,1,3], "dimensionless")
rf_area_pi = Square(duration=q0.parameters.rf.pi_duration,amplitude=rf_amplitudes)
rf_area_half = Square(duration=q0.parameters.rf.pi2_duration,amplitude=rf_amplitudes)

def rf_area_amplification(q, amplitude, blocks, reference_half, test_pulse, *, pulses_per_block, prepare, readout, shots=2):
    exp = Experiment(f"rf_area_{pulses_per_block}_pulse_blocks",[q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(blocks), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,reference_half)
            with exp.repeat(blocks):
                for _ in range(pulses_per_block):
                    play(q.rf,test_pulse)
        readout()
    return exp

area_pi_preview = lab.compile(rf_area_amplification(q0,rf_amplitudes,area_blocks,rf_pi2,rf_area_pi,pulses_per_block=2,**callbacks))
area_half_preview = lab.compile(rf_area_amplification(q0,rf_amplitudes,area_blocks,rf_pi2,rf_area_half,pulses_per_block=4,**callbacks))
model_detuning_hz = np.linspace(-4000,4000,101)
model_rabi_time_s = np.linspace(0,800e-6,121)
model_ramsey_time_s = np.linspace(0,2e-3,121)
model_rabi_hz = 1/(2*q0.parameters.rf.pi_duration)
model_chevron = analysis.rabi_probability(model_rabi_time_s[None,:],model_detuning_hz[:,None],model_rabi_hz)
model_ramsey = analysis.ramsey_probability(model_ramsey_time_s[None,:],model_detuning_hz[:,None],model_rabi_hz,q0.parameters.rf.pi2_duration)
fig,axes = plt.subplots(1,2,figsize=(10,3))
for ax,times,values,title in zip(axes,[model_rabi_time_s,model_ramsey_time_s],[model_chevron,model_ramsey],["MODEL Rabi chevron","MODEL finite-pulse Ramsey"]):
    im = ax.pcolormesh(times*1e6,model_detuning_hz/1e3,values,shading="auto",vmin=0,vmax=1)
    ax.set(xlabel="Pulse / dark time (us)",ylabel="Drive - transition (kHz)",title=title)
    fig.colorbar(im,ax=ax,label="P1")
fig.savefig(OUTPUT/"model_2d_rabi_ramsey.png",dpi=150,bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT/"model_2d_rabi_ramsey.png")))
""")
md(r"""
## 8. Global-RF single-qubit randomized benchmarking

Sample all 24 single-qubit Cliffords and freeze the seed and exact inverse. Broadcast each sequence to all atoms and fit $P_0(m)=A p^m+B$ per site. Under the usual Markovian, weakly gate-dependent and low-leakage assumptions, the average error per Clifford is $r_C=(1-p)/2$. A primitive-pulse fidelity requires additional assumptions. Sites receive the same sequence. Independent local gates in a two-qubit Clifford catalog cannot be implemented with this broadcast alone.

The notebook constructs the 2×2 matrices, Clifford catalog, user basis, decompositions, random sequences, recovery, native gate words and length × sequence sweep. For acquisition, start with 8–12 depths and tens of sequences per depth, then extend after a pilot decay. Preserve seed, sequence, site and shot and bootstrap whole sequences. We freeze 48 circuits and compile a preview of three lengths × two sequences.

### 8.1 From 2×2 unitaries to exact Bloch rotations

For $U_a(\theta)=\cos(\theta/2)I-i\sin(\theta/2)\sigma_a$ and $\rho=(I+\mathbf r\cdot\boldsymbol\sigma)/2$, the action $U\rho U^\dagger$ gives $R_{ij}=\mathrm{Tr}(\sigma_i U\sigma_j U^\dagger)/2$ and $\mathbf r'=R\mathbf r$. Global phase cancels. Check that the rotation is a Clifford before rounding its entries to −1, 0 and 1. Angles are radians and gate lists are in execution order, so A followed by B has matrix B @ A. These matrices specify the target rotation.
""")
code(
    r'''
from collections import deque
from dataclasses import fields, is_dataclass
from fractions import Fraction
from hashlib import sha256
from heapq import heappop, heappush

from scipy.optimize import least_squares

from taiyidsl import FlatTopGaussian, rz
from taiyidsl.units import Quantity, as_fraction

RB_ATOL = 1e-10
RB_PAULI = {
    "x": np.array([[0, 1], [1, 0]], dtype=complex),
    "y": np.array([[0, -1j], [1j, 0]], dtype=complex),
    "z": np.array([[1, 0], [0, -1]], dtype=complex),
}


def rb_rotation(axis, angle):
    """Return a 2×2 rotation about a Pauli axis for an angle in radians."""
    if axis not in RB_PAULI or not np.isfinite(angle):
        raise ValueError("Require axis x/y/z and a finite angle")
    return np.cos(angle / 2) * np.eye(2) - 1j * np.sin(angle / 2) * RB_PAULI[axis]


def rb_rf_unitary(angle, phase):
    """Rotate by angle around cos(phase) X + sin(phase) Y."""
    if not np.isfinite([angle, phase]).all():
        raise ValueError("RF angle and phase must be finite")
    axis = np.cos(phase) * RB_PAULI["x"] + np.sin(phase) * RB_PAULI["y"]
    return np.cos(angle / 2) * np.eye(2) - 1j * np.sin(angle / 2) * axis


def chronological_unitary(unitaries):
    """Multiply on the left in execution order. An empty sequence gives I."""
    total = np.eye(2, dtype=complex)
    for unitary in unitaries:
        total = unitary @ total
    return total


def same_unitary(left, right):
    """Compare unitaries up to a single global phase, preserving relative phase."""
    overlap = np.trace(np.asarray(right).conj().T @ left) / 2
    if abs(overlap) < RB_ATOL:
        return False
    return np.allclose(left, overlap / abs(overlap) * right, atol=RB_ATOL, rtol=0)


def unitary_to_rotation(unitary):
    """Check unitarity and Clifford action before returning an exact integer rotation.

    Column j gives the Pauli coefficients of U sigma_j U†. Accept only
    right-handed signed axis permutations. For example, Rz(pi/4) raises.
    """
    unitary = np.asarray(unitary, dtype=complex)
    if unitary.shape != (2, 2) or not np.isfinite(unitary).all():
        raise ValueError("Require a finite 2x2 unitary")
    if not np.allclose(unitary.conj().T @ unitary, np.eye(2), atol=RB_ATOL, rtol=0):
        raise ValueError("Matrix is not unitary")
    axes = tuple(RB_PAULI.values())
    rotation = np.array([
        [np.trace(a @ unitary @ b @ unitary.conj().T) / 2 for b in axes]
        for a in axes
    ])
    rounded = np.rint(rotation.real).astype(int)
    if (
        not np.allclose(rotation, rounded, atol=RB_ATOL, rtol=0)
        or not np.array_equal(rounded.T @ rounded, np.eye(3, dtype=int))
        or round(np.linalg.det(rounded)) != 1
    ):
        raise ValueError("Basis gates must be single-qubit Cliffords")
    return rounded


def rotation_key(rotation):
    """Flatten the exact rotation row by row for deduplication and inverse lookup."""
    return tuple(int(v) for v in rotation.flat)
''',
    tags=("rb-definitions",),
)
md(r"""
### 8.2 A fixed catalog of 24 Cliffords

Starting at I, breadth-first search records the first word reaching each rotation using reference XY generators. Six possible X directions and four perpendicular Y directions give 24 elements. Preserving generator order preserves the project's historical indexing and seeded logical circuits. The catalog stays fixed when the RF basis changes. Recompute only the gate decompositions. Identity has an empty word.
""")
code(
    r'''
RF_REFERENCE_UNITARIES = {
    "x90": rb_rotation("x", np.pi / 2),
    "xm90": rb_rotation("x", -np.pi / 2),
    "y90": rb_rotation("y", np.pi / 2),
    "ym90": rb_rotation("y", -np.pi / 2),
    "x180": rb_rotation("x", np.pi),
    "y180": rb_rotation("y", np.pi),
}


def build_clifford_catalog():
    """Enumerate rotations and stable reference words by BFS over the XY generators.

    The queue visits short words first. Deduplicate on matrices.
    Optical RB retains this reference word. RF gates are optimized separately.
    """
    generators = {name: unitary_to_rotation(u) for name, u in RF_REFERENCE_UNITARIES.items()}
    identity = np.eye(3, dtype=int)
    queue = deque([(identity, ())])
    visited = {rotation_key(identity)}
    catalog = []
    while queue:
        rotation, word = queue.popleft()
        catalog.append({"rotation": rotation.copy(), "reference_word": word})
        for name, gate in generators.items():
            candidate = gate @ rotation
            key = rotation_key(candidate)
            if key not in visited:
                visited.add(key)
                queue.append((candidate, word + (name,)))
    if len(catalog) != 24:
        raise RuntimeError("The fixed reference generators must produce 24 Cliffords")
    return tuple(catalog)


def axis_images(rotation):
    """Express the three columns as signed axis names such as +X, +Z, -Y."""
    return tuple(
        ("+" if column[np.argmax(abs(column))] > 0 else "-")
        + "XYZ"[np.argmax(abs(column))]
        for column in rotation.T
    )
''',
    tags=("rb-definitions",),
)
code(r"""
def show_rb_table(rows):
    '''Render a Markdown table so long gate words can be compared row by row.'''
    columns = tuple(rows[0])
    lines = [" | ".join(columns), " | ".join("---" for _ in columns)]
    for row in rows:
        lines.append(" | ".join(str(row[c]).replace("|", r"\|") for c in columns))
    display(Markdown("\n".join(lines)))


rb_catalog = build_clifford_catalog()
show_rb_table([
    {"Clifford": i, "X/Y/Z images": axis_images(c["rotation"]),
     "reference word": " → ".join(c["reference_word"]) or "I"}
    for i, c in enumerate(rb_catalog)
])
""")
md(r"""
### 8.3 Specify each basis gate's ideal matrix, implementation and cost

Provide a unitary and its time-ordered implementation. RF steps cite named calibrated pulses, target angles and drive phases. Negative X uses positive area with a π phase shift. A virtual-Z step calls `rz` with its angle. Check that the product of each gate's steps gives the declared unitary up to global phase. Supported calibrations here are fixed unmodulated Square or FlatTopGaussian pulses.

Compare paths by (RF pulse count, total RF duration, total steps). `Fraction` preserves exact durations. Virtual Z costs (0, 0, 1). Positive and negative π can share a Clifford while using distinct calibrations. Basis order breaks ties. The empty word already supplies identity.
""")
code(
    r'''
def rb_json_value(value):
    """Save the numerical settings of a static pulse for the experiment record."""
    if isinstance(value, Quantity):
        return {"value": float(value), "dimension": value.dimension}
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, np.ndarray):
        return rb_json_value(value.tolist())
    if isinstance(value, np.generic):
        return rb_json_value(value.item())
    if is_dataclass(value):
        return {"type": type(value).__name__,
                **{f.name: rb_json_value(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {key: rb_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rb_json_value(item) for item in value]
    return value


def rb_pulse_duration(pulse):
    """Read the exact pulse duration in seconds from its named calibration."""
    if isinstance(pulse, Square):
        duration = as_fraction(pulse.duration, "time")
    elif isinstance(pulse, FlatTopGaussian):
        duration = as_fraction(pulse.flat_duration, "time") + (
            2 * as_fraction(pulse.sigma, "time")
            * as_fraction(pulse.cutoff_sigma, "dimensionless")
        )
    else:
        raise ValueError("RB requires a static Square or FlatTopGaussian pulse")
    if duration <= 0 or pulse.phase_modulation is not None:
        raise ValueError("RB pulses need positive duration and no phase modulation")
    # Reject swept amplitude/frequency too: a frozen word needs fixed calibration.
    for value, dimension in ((pulse.amplitude, "dimensionless"), (pulse.frequency, "frequency")):
        if value is not None:
            as_fraction(value, dimension)
    return duration


def prepare_gate_basis(definitions, pulses):
    """Check the ideal and physical gates and derive costs and integer rotations.

    Map names to calibrated pulses. Each RF angle is a calibration target
    and phase explicitly overrides play. Only the complete basis gate must
    be Clifford. Its intermediate rotations may be non-Clifford.
    """
    if not definitions:
        raise ValueError("Gate basis must not be empty")
    basis = {}
    for name, definition in definitions.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Gate names must be nonempty strings")
        unitary = np.asarray(definition["unitary"], dtype=complex)
        rotation = unitary_to_rotation(unitary)
        implementation = tuple(dict(step) for step in definition["implementation"])
        if not implementation:
            raise ValueError(f"{name}: use the catalogue's empty word for identity")
        step_unitaries = []
        pulse_count, duration = 0, Fraction(0)
        for step in implementation:
            angle = float(step["angle"])
            if not np.isfinite(angle):
                raise ValueError(f"{name}: angle must be finite")
            if step["kind"] == "rf":
                if angle <= 0:
                    raise ValueError("Use positive RF area and phase to choose the axis")
                phase = float(step["phase"])
                step_unitaries.append(rb_rf_unitary(angle, phase))
                pulse_count += 1
                duration += rb_pulse_duration(pulses[step["pulse"]])
            elif step["kind"] == "virtual_z":
                step_unitaries.append(rb_rotation("z", angle))
            else:
                raise ValueError(f"Unknown implementation kind: {step['kind']}")
        if not same_unitary(chronological_unitary(step_unitaries), unitary):
            raise ValueError(f"{name}: implementation does not match the declared unitary")
        basis[name] = {
            "unitary": unitary.copy(), "rotation": rotation,
            "implementation": implementation,
            "cost": (pulse_count, duration, len(implementation)),
            "pulse_snapshots": {
                step["pulse"]: rb_json_value(pulses[step["pulse"]])
                for step in implementation if step["kind"] == "rf"
            },
        }
    return basis


def rb_basis_fingerprint(basis):
    """Bind gate definitions, implementation, pulses and order to the frozen basis."""
    payload = [
        {"name": name, "rotation": gate["rotation"].tolist(),
         "implementation": gate["implementation"], "cost": gate["cost"],
         "pulse_snapshots": gate["pulse_snapshots"]}
        for name, gate in basis.items()
    ]
    encoded = json.dumps(rb_json_value(payload), sort_keys=True, allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def optimize_clifford_decompositions(catalog, basis):
    """Find the lexicographically cheapest paths on the fixed 24-node graph.

    Appending a gate produces gate @ current. Dijkstra expands the
    cheapest accumulated path. Virtual Z counts as one step, so cycles cost
    more than their prefixes. Only strict improvements replace a path.
    Serial and insertion order resolve ties. Each Clifford is optimized independently.
    """
    lookup = {rotation_key(c["rotation"]): i for i, c in enumerate(catalog)}
    costs, words = {0: (0, Fraction(0), 0)}, {0: ()}
    queue, serial = [(costs[0], 0, 0)], 0
    while queue:
        cost, _, index = heappop(queue)
        if costs[index] != cost:
            continue
        for name, gate in basis.items():
            target = lookup[rotation_key(gate["rotation"] @ catalog[index]["rotation"])]
            candidate = tuple(a + b for a, b in zip(cost, gate["cost"], strict=True))
            if target not in costs or candidate < costs[target]:
                costs[target] = candidate
                words[target] = words[index] + (name,)
                serial += 1
                heappush(queue, (candidate, serial, target))
    if len(costs) != 24:
        raise ValueError(f"Basis reaches only {len(costs)}/24 Cliffords")
    return tuple({"native_word": words[i], "cost": costs[i]} for i in range(24))
''',
    tags=("rb-definitions",),
)
code("""
# Rebuild the named pulses from section 2 after calibration changes and rerun this cell.
rb_pulses = {"rf_pi": rf_pi, "rf_pi2": rf_pi2}
rf_xy_basis = {
    "x90": {"unitary": rb_rotation("x", np.pi/2),
            "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": 0}]},
    "xm90": {"unitary": rb_rotation("x", -np.pi/2),
             "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": np.pi}]},
    "y90": {"unitary": rb_rotation("y", np.pi/2),
            "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": np.pi/2}]},
    "ym90": {"unitary": rb_rotation("y", -np.pi/2),
             "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": -np.pi/2}]},
    "x180": {"unitary": rb_rotation("x", np.pi),
             "implementation": [{"kind": "rf", "pulse": "rf_pi", "angle": np.pi, "phase": 0}]},
    "y180": {"unitary": rb_rotation("y", np.pi),
             "implementation": [{"kind": "rf", "pulse": "rf_pi", "angle": np.pi, "phase": np.pi/2}]},
}
rf_xvz_basis = {
    "x90": rf_xy_basis["x90"],
    "xm90": rf_xy_basis["xm90"],
    "x180": rf_xy_basis["x180"],
    "xm180": {"unitary": rb_rotation("x", -np.pi),
              "implementation": [{"kind": "rf", "pulse": "rf_pi", "angle": np.pi, "phase": np.pi}]},
    "z90": {"unitary": rb_rotation("z", np.pi/2),
            "implementation": [{"kind": "virtual_z", "angle": np.pi/2}]},
    "zm90": {"unitary": rb_rotation("z", -np.pi/2),
             "implementation": [{"kind": "virtual_z", "angle": -np.pi/2}]},
    "z180": {"unitary": rb_rotation("z", np.pi),
             "implementation": [{"kind": "virtual_z", "angle": np.pi}]},
}

# Select a basis here or supply another dictionary with the same structure.
RB_BASIS_NAME = "X/VZ"
gate_basis = {"XY": rf_xy_basis, "X/VZ": rf_xvz_basis}[RB_BASIS_NAME]
rb_basis = prepare_gate_basis(gate_basis, rb_pulses)
rb_decompositions = optimize_clifford_decompositions(rb_catalog, rb_basis)
rb_xy_basis = prepare_gate_basis(rf_xy_basis, rb_pulses)
rb_xy_decompositions = optimize_clifford_decompositions(rb_catalog, rb_xy_basis)
rb_xvz_basis = prepare_gate_basis(rf_xvz_basis, rb_pulses)
rb_xvz_decompositions = optimize_clifford_decompositions(rb_catalog, rb_xvz_basis)

decomposition_comparison = []
for index in range(24):
    row = {"Clifford": index, "X/Y/Z images": axis_images(rb_catalog[index]["rotation"])}
    for label, table in (("XY", rb_xy_decompositions), ("X/VZ", rb_xvz_decompositions)):
        word, cost = table[index]["native_word"], table[index]["cost"]
        row.update({
            f"{label} word": " → ".join(word) or "I",
            f"{label} RF pulses": cost[0], f"{label} VZ": cost[2] - cost[0],
            f"{label} duration (us)": float(cost[1]) * 1e6,
        })
    decomposition_comparison.append(row)
show_rb_table(decomposition_comparison)
show_rb_table([
    {"basis": label, "mean RF pulses / Clifford": np.mean([d["cost"][0] for d in table]),
     "mean duration (us) / Clifford": np.mean([float(d["cost"][1]) * 1e6 for d in table])}
    for label, table in (("XY", rb_xy_decompositions), ("X/VZ", rb_xvz_decompositions))
])
""")
md(r"""
### 8.3.1 Why the default decomposition averages 0.833 RF pulses

| Zero-based catalog indices | RF implementation in the X/VZ basis | Count |
|---|---|---:|
| 0, 19, 21, 23 | I, z180, zm90, z90, no RF | 4 |
| 1–4, 7–18 | One RF π/2, possibly with virtual Z | 16 |
| 5, 6, 20, 22 | One RF π, possibly with virtual Z | 4 |

Uniform sampling gives $(4\times0+16+4)/24=5/6\approx0.8333$ physical RF pulses per catalog element. In the XY table, 1, 6, 13 and 4 elements require 0, 1, 2 and 3 pulses, averaging 1.8333. With 100 µs half pulses and 200 µs π pulses the X/VZ mean duration is $(16\times100+4\times200)/24=100$ µs. Recompute after changing the calibration or basis.

Finite random circuits have their own mean. RB depth counts random Cliffords and excludes recovery, whose decomposition adds pulses to the executed circuit. The compiler here performs no cross-Clifford simplification. Dividing a fitted Clifford error by 0.833 does not establish a pulse error.
""")
md(r"""
### 8.3.2 Dijkstra's decomposition of Clifford #15

Clifford #15 maps X/Y/Z to (+Z, −Y, +X), a Hadamard up to global phase. Read the arrows in execution order.

| Basis | Word | RF count | RF duration | Steps |
|---|---|---:|---:|---:|
| XY | y90 → x180 | 2 | 300 µs | 2 |
| X/VZ | z90 → x90 → z90 | 1 | 100 µs | 3 |

With $R_a(\theta)=e^{-i\theta\sigma_a/2}$, $R_x(\pi)R_y(\pi/2)=R_z(\pi/2)R_x(\pi/2)R_z(\pi/2)=-iH$ and $H=(X+Z)/\sqrt2$. Adding a basis gate to the current word moves to `gate_rotation @ current_rotation` in the 24-node graph. Dijkstra compares cumulative costs lexicographically. Along the chosen X/VZ path they are (0,0,0), (0,0,1), (1,100,2), then (1,100,3), with time in µs. The last cost beats (2,300,2) for XY. Every virtual Z costs one step and declaration order resolves exact ties.

Pure Z rotations preserve the Z axis. Mapping Z to X therefore requires at least one RF pulse. This word meets that lower bound under the declared cost. Measured error needs separate characterization. From frame F=0, the first z90 gives −90°, the RF axis becomes −90°, and the last z90 leaves −180°. The implemented gate includes this frame. Section 8.5.2 follows the same gate after a preceding Clifford.
""")
md(r"""
### 8.4 Sample logical Cliffords before expanding the chosen basis

Length counts random Cliffords before recovery. Each (seed, length, sequence) initializes an independent RNG and samples 0…23 uniformly with replacement. Lengths need not share prefixes, two samples can coincide and extra shots replay the same sequence. For $T=C_m\cdots C_1$, recovery is $T^{-1}=T^T$. Look up that index in the fixed catalog and expand it in the same basis. Changing the basis preserves every random and recovery index.
""")
code(
    r'''
def rb_integer_values(values, label):
    """Require unique nonnegative integer coordinates. Reject booleans and floats."""
    values = tuple(values)
    if not values or any(
        isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) or v < 0
        for v in values
    ) or len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique nonnegative integers")
    return tuple(int(v) for v in values)


def sample_rb_circuits(lengths, *, sequence_count, seed, catalog):
    """Freeze logical circuits independently of basis, shot and scan order."""
    lengths = rb_integer_values(lengths, "lengths")
    if type(sequence_count) is not int or sequence_count <= 0:
        raise ValueError("sequence_count must be a positive integer")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    lookup = {rotation_key(c["rotation"]): i for i, c in enumerate(catalog)}
    circuits = []
    for length in lengths:
        for sequence in range(sequence_count):
            rng = np.random.default_rng(np.random.SeedSequence([seed, length, sequence]))
            indices = [int(i) for i in rng.integers(0, len(catalog), size=length)]
            total = np.eye(3, dtype=int)
            for index in indices:
                total = catalog[index]["rotation"] @ total
            circuits.append({
                "seed": seed, "length": length, "sequence": sequence,
                "clifford_indices": indices,
                "recovery_index": lookup[rotation_key(total.T)],
            })
    return circuits


def decompose_rb_circuits(circuits, catalog, decompositions, basis):
    """Expand each Clifford and its recovery while preserving gate boundaries."""
    output = []
    basis_fingerprint = rb_basis_fingerprint(basis)
    for circuit in circuits:
        indices = [*circuit["clifford_indices"], circuit["recovery_index"]]
        words, total = [], np.eye(3, dtype=int)
        for index in indices:
            word = decompositions[index]["native_word"]
            rotation = np.eye(3, dtype=int)
            for name in word:
                rotation = basis[name]["rotation"] @ rotation
            if not np.array_equal(rotation, catalog[index]["rotation"]):
                raise ValueError(f"Decomposition does not implement Clifford {index}")
            total = rotation @ total
            words.append(list(word))
        if not np.array_equal(total, np.eye(3, dtype=int)):
            raise ValueError("RB recovery is not the ideal identity")
        native_word = [name for word in words for name in word]
        cost = tuple(sum((basis[name]["cost"][i] for name in native_word), Fraction(0) if i == 1 else 0)
                     for i in range(3))
        output.append({
            **circuit, "clifford_indices": list(circuit["clifford_indices"]),
            "basis_fingerprint": basis_fingerprint,
            "clifford_words": words, "native_word": native_word,
            "rf_pulse_count": cost[0], "virtual_z_count": cost[2] - cost[0],
            "pulse_duration_s": float(cost[1]),
        })
    return output


def rb_basis_snapshot(basis, pulses):
    """Save gate identity, step order, exact cost and named pulse calibrations."""
    used = {s["pulse"] for gate in basis.values() for s in gate["implementation"] if s["kind"] == "rf"}
    return {
        "fingerprint": rb_basis_fingerprint(basis),
        "cost_order": ["rf_pulse_count", "pulse_duration_s", "implementation_step_count"],
        "gates": {
            name: {"rotation": gate["rotation"].tolist(),
                   "unitary_real": gate["unitary"].real.tolist(),
                   "unitary_imag": gate["unitary"].imag.tolist(),
                   "implementation": rb_json_value(gate["implementation"]),
                   "cost": rb_json_value(gate["cost"])}
            for name, gate in basis.items()
        },
        "pulses": {name: rb_json_value(pulse) for name, pulse in pulses.items() if name in used},
    }
''',
    tags=("rb-definitions",),
)
code("""
RB_LENGTHS = (0, 1, 4, 8, 16, 32)
RB_SEQUENCE_COUNT = 8
RB_SEED = 171
rb_logical_circuits = sample_rb_circuits(
    RB_LENGTHS, sequence_count=RB_SEQUENCE_COUNT, seed=RB_SEED, catalog=rb_catalog
)
rb_sequences = decompose_rb_circuits(rb_logical_circuits, rb_catalog, rb_decompositions, rb_basis)
rb_short = next(s for s in rb_sequences if s["length"] == 4 and s["sequence"] == 0)
show_rb_table([
    {"position": i, "role": "recovery" if i == rb_short["length"] else "random",
     "Clifford": index, "native word": " → ".join(word) or "I"}
    for i, (index, word) in enumerate(zip(
        [*rb_short["clifford_indices"], rb_short["recovery_index"]],
        rb_short["clifford_words"], strict=True
    ))
])
print("Native execution order:", " → ".join(rb_short["native_word"]))
print("RF pulses:", rb_short["rf_pulse_count"], "; virtual Z:", rb_short["virtual_z_count"])
""")
md(r"""
### 8.5 Build and preview the experiment

Consume frozen circuits. Each condition prepares the atoms, waits an explicit 10 µs, executes its gate word within one AWG block and reads out. RF broadcasts q0's calibration once to all registered atoms. `rz(q, θ)` subtracts θ from the persistent spin-drive frame. Subsequent RF uses its base phase plus that frame. Avoid a second phase correction or reset between gates. Retain trailing virtual Z for later phase-sensitive control. The backend checks physical update constraints.

The preview uses `lengths=(0,1,4)`, `sequence_ids=(0,1)` and two shots per point. A full 48-point experiment is built and can be compiled using `COMPILE_FULL_RB`. After changing a basis or calibration, regenerate its decompositions, expand the frozen circuits and rebuild.
""")
code(
    r'''
def global_rf_rb(q, circuits, basis, pulses, *, lengths, sequence_ids, prepare, readout, shots=2):
    """Build a global RF RB experiment using frozen gates.

    Read native_word from each circuit and use the supplied basis for its RF/VZ steps.
    The circuits must cover lengths × sequence_ids. Depth counts Cliffords.
    At depth zero, prepare and read out without adding an identity slot.
    """
    lengths = rb_integer_values(lengths, "lengths")
    sequence_ids = rb_integer_values(sequence_ids, "sequence_ids")
    records = {(c["length"], c["sequence"]): c for c in circuits}
    if len(records) != len(circuits):
        raise ValueError("Duplicate frozen RB coordinates")
    if any((m, s) not in records for m in lengths for s in sequence_ids):
        raise ValueError("Missing frozen circuit for a requested length/sequence")
    selected = [records[m, s] for m in lengths for s in sequence_ids]
    # Re-expand after changing a gate or pulse, even if its cost is unchanged.
    basis = prepare_gate_basis(basis, pulses)
    fingerprint = rb_basis_fingerprint(basis)
    if any(c.get("basis_fingerprint") != fingerprint for c in selected):
        raise ValueError("Gate basis or pulses changed: rebuild decompositions and circuits")
    exp = Experiment("global_rf_1q_rb", [q], metadata={
        "control": "same RF on every registered atom",
        "length_definition": "random Cliffords excluding recovery",
        "basis": rb_basis_snapshot(basis, pulses),
        "rb_sequences": selected, "frozen_circuit_count": len(circuits),
        "compiled_condition_selection": {"lengths": list(lengths), "sequence_ids": list(sequence_ids)},
        "hardware_executed": False,
    })
    q = exp.qubits[q.uid]
    exp.metadata["reference_qubit_snapshot"] = q.to_dict()
    length_axis = SweepParameter("rb_length", lengths, "dimensionless")
    sequence_axis = SweepParameter("rb_sequence", sequence_ids, "dimensionless")
    with exp.average(shots), exp.sweep(length_axis), exp.sweep(sequence_axis), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            for length in lengths:
                with exp.case(length_axis, length):
                    for sequence_id in sequence_ids:
                        with exp.case(sequence_axis, sequence_id):
                            for gate_name in records[length, sequence_id]["native_word"]:
                                for step in basis[gate_name]["implementation"]:
                                    if step["kind"] == "rf":
                                        play(q.rf, pulses[step["pulse"]], phase=step["phase"])
                                    else:  # validated virtual_z
                                        rz(q, step["angle"])
        readout()
    return exp
''',
    tags=("rb-definitions",),
)
code("""
RB_PREVIEW_LENGTHS = (0, 1, 4)
RB_PREVIEW_SEQUENCE_IDS = (0, 1)
COMPILE_FULL_RB = False

rb_experiment = global_rf_rb(
    q0, rb_sequences, rb_basis, rb_pulses,
    lengths=RB_LENGTHS, sequence_ids=range(RB_SEQUENCE_COUNT), **callbacks,
)
rb_preview_experiment = global_rf_rb(
    q0, rb_sequences, rb_basis, rb_pulses,
    lengths=RB_PREVIEW_LENGTHS, sequence_ids=RB_PREVIEW_SEQUENCE_IDS, **callbacks,
)
rb_preview = lab.compile(rb_preview_experiment)
rb_full_compiled = lab.compile(rb_experiment) if COMPILE_FULL_RB else None
rb_manifest = {
    "catalog": "fixed_xy_24_reference_order", "basis_name": RB_BASIS_NAME,
    "basis": rb_basis_snapshot(rb_basis, rb_pulses),
    "reference_qubit_snapshot": rb_experiment.metadata["reference_qubit_snapshot"],
    "clifford_catalog": [
        {"index": i, "rotation": c["rotation"].tolist(), "reference_word": list(c["reference_word"])}
        for i, c in enumerate(rb_catalog)
    ],
    "decompositions": rb_json_value(rb_decompositions),
    "circuits": rb_sequences,
    "preview": {"lengths": list(RB_PREVIEW_LENGTHS), "sequence_ids": list(RB_PREVIEW_SEQUENCE_IDS),
                "compiled_points": len(rb_preview.points), "shots": rb_preview.shots},
    "full_compiled": rb_full_compiled is not None,
    "hardware_executed": False,
}
(OUTPUT / "rf_rb_sequences.json").write_text(
    json.dumps(rb_manifest, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8"
)
display({"frozen_circuits": len(rb_sequences), "preview_points": len(rb_preview.points),
         "preview_shots": rb_preview.shots, "full_compiled": rb_manifest["full_compiled"]})

# Inspect compiled conditions and the RF broadcast sites from the existing artifact.
assert [p.axis_indices for p in rb_preview.points] == [(i, j) for i in range(3) for j in range(2)]
for point in rb_preview.points:
    record = next(c for c in rb_sequences
                  if (c["length"], c["sequence"]) == (point.coordinates["rb_length"], point.coordinates["rb_sequence"]))
    events = [e for e in point.logical_events if e.kind == "play" and e.signal == q0.rf]
    assert len(events) == record["rf_pulse_count"]
    assert all(e.params["global_operation"]["targets"] == ["q0", "q1", "spectator"] for e in events)
    assert sum(e.kind == "rz" for e in point.logical_events) == record["virtual_z_count"]
    assert sum(e.kind == "reset_phase" for e in point.logical_events) == 1
    if record["length"] == 0:
        assert not events
""")
md(r"""
### 8.5.1 Compare pulse sheets and final DAC waveforms

Both exports use the same random Clifford and recovery indices, six conditions and two shots. They may have different RF counts, durations and waveforms.

| Basis | Overview | Pulse sheet | DAC waveform |
|---|---|---|---|
| XY | [Report](../../output/yb171_tuneup/rf_rb_basis_comparison/xy/index.html) | [Timing](../../output/yb171_tuneup/rf_rb_basis_comparison/xy/pulse_sheet.html) | [DAC](../../output/yb171_tuneup/rf_rb_basis_comparison/xy/waveform.html) |
| X/VZ | [Report](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/index.html) | [Timing](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/pulse_sheet.html) | [DAC](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/waveform.html) |

In Advanced select point 4 (length 4, sequence 0). Both bases use [19,15,13,1], then recovery 19. The pulse sheet shows zero-duration rz markers. A length-zero circuit has no RF waveform. **AWG color** switches between channel and pulse colors. Hover over pulses for native gate, Clifford index and recovery. Dashed vertical lines with triangular ends display virtual Z angles and frame updates. A single marker lists coincident updates in execution order. X/VZ point 4 has five updates at four times.

Each report includes `phase_trace.csv/json` matching native steps to compiled logical events. The RF axis phase excludes the time-dependent carrier term. The trace retains the trailing virtual Z. These are compiled records, with no measured RB fidelity or simulated quantum populations.
""")
code(
    r"""
import csv

RB_REPORT_ROOT = OUTPUT / "rf_rb_basis_comparison"
rb_basis_previews, rb_basis_reports = {}, {}
rb_report_summary = {}
for label, slug, basis, table in (
    ("XY", "xy", rb_xy_basis, rb_xy_decompositions),
    ("X/VZ", "xvz", rb_xvz_basis, rb_xvz_decompositions),
):
    circuits = decompose_rb_circuits(rb_logical_circuits, rb_catalog, table, basis)
    assert all(
        (left["clifford_indices"], left["recovery_index"])
        == (right["clifford_indices"], right["recovery_index"])
        for left, right in zip(circuits, rb_sequences, strict=True)
    )
    if rb_basis_fingerprint(basis) == rb_basis_fingerprint(rb_basis):
        compiled = rb_preview
    else:
        comparison_experiment = global_rf_rb(
            q0, circuits, basis, rb_pulses,
            lengths=RB_PREVIEW_LENGTHS, sequence_ids=RB_PREVIEW_SEQUENCE_IDS,
            **callbacks,
        )
        compiled = lab.compile(comparison_experiment)
    rb_basis_previews[label] = compiled
    directory = RB_REPORT_ROOT / slug
    rb_basis_reports[label] = compiled.export_report(directory)
    compiled.assert_integrity()

    by_condition = {(c["length"], c["sequence"]): c for c in circuits}
    phase_trace, conditions = [], []
    for point in compiled.points:
        record = by_condition[point.coordinates["rb_length"], point.coordinates["rb_sequence"]]
        conditions.append({
            "point": point.index, "length": record["length"], "sequence": record["sequence"],
            "clifford_indices": record["clifford_indices"],
            "recovery_index": record["recovery_index"],
            "rf_pulses": record["rf_pulse_count"], "virtual_z": record["virtual_z_count"],
            "duration_us": record["pulse_duration_s"] * 1e6,
        })
        steps = [
            (position, index, name, step)
            for position, (index, word) in enumerate(zip(
                [*record["clifford_indices"], record["recovery_index"]],
                record["clifford_words"], strict=True,
            ))
            for name in word for step in basis[name]["implementation"]
        ]
        events = [
            event for event in point.logical_events
            if event.kind == "rz" or (event.kind == "play" and event.signal == q0.rf)
        ]
        origin = next(e.start_mu for e in point.logical_events if e.kind == "reset_phase")
        frame = 0.0
        for (position, index, name, step), event in zip(steps, events, strict=True):
            before, base, effective = frame, None, None
            if step["kind"] == "virtual_z":
                assert event.kind == "rz"
                np.testing.assert_allclose(event.params["angle"], step["angle"], atol=1e-12, rtol=0)
                frame -= step["angle"]
            else:
                assert event.kind == "play"
                base, effective = step["phase"], event.params["phase"]
                np.testing.assert_allclose(effective, base + frame, atol=1e-12, rtol=0)
            phase_trace.append({
                "point": point.index, "length": record["length"], "sequence": record["sequence"],
                "clifford_position": position, "clifford_index": index,
                "role": "recovery" if position == record["length"] else "random",
                "gate": name, "kind": step["kind"],
                "start_us_from_rb_entry": (event.start_mu - origin) * compiled.setup.tick_seconds * 1e6,
                "duration_us": event.duration_mu * compiled.setup.tick_seconds * 1e6,
                "angle_deg": float(np.rad2deg(step["angle"])),
                "frame_before_deg": float(np.rad2deg(before)),
                "frame_after_deg": float(np.rad2deg(frame)),
                "base_phase_deg": None if base is None else float(np.rad2deg(base)),
                "compiled_phase_deg": None if effective is None else float(np.rad2deg(effective)),
                "compiled_phase_mod360_deg": None if effective is None else float(np.rad2deg(effective) % 360),
            })
    (directory / "phase_trace.json").write_text(
        json.dumps(phase_trace, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    if phase_trace:
        with (directory / "phase_trace.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(phase_trace[0]))
            writer.writeheader()
            writer.writerows(phase_trace)
    rb_report_summary[label] = {
        "basis": rb_basis_snapshot(basis, rb_pulses),
        "conditions": conditions, "shots": compiled.shots,
        "mean_rf_pulses_per_clifford": sum(d["cost"][0] for d in table) / 24,
        "report": str(rb_basis_reports[label]["index"].relative_to(OUTPUT)),
        "hardware_executed": False,
    }
(RB_REPORT_ROOT / "comparison.json").write_text(
    json.dumps(rb_report_summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
)
show_rb_table([
    {"basis": label, **row}
    for label, summary in rb_report_summary.items()
    for row in summary["conditions"] if row["length"] == 4 and row["sequence"] == 0
])
""",
    tags=("rb-report-export",),
)
md(r"""
### 8.5.2 Follow the cumulative virtual-Z phase at point 4

For the default seed 171 and preview order, select point 4, shot 0. If you change sweep order, look up the length and sequence coordinates. Compare the [CSV](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/phase_trace.csv) or [JSON](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/phase_trace.json) written by the previous cell. In both words the random indices are [19,15,13,1] and the final group is recovery #19. The vertical separators neither consume time nor reset the frame.

```text
Clifford：  #19          | #15          | #13       | #1  | #19 recovery
XY：        x180 y180    | y90 x180     | y90 x90   | x90 | x180 y180
X/VZ：      z180         | z90 x90 z90  | x90 z90   | x90 | z180
```

Start at $F=0$. A virtual $R_z(\theta)$ updates $F\leftarrow F-\theta$. RF uses $\phi_{\rm RF}=\phi_{\rm base}+F$. Positive z90 subtracts 90° from subsequent RF axes and zm90 adds 90°. The DAC also accumulates carrier phase with time. Each x90 in this word has base phase 0°.

| Step | Clifford | Native operation | Start (µs) | F before → after | RF axis modulo 360° |
|---|---|---|---:|---|---|
| 1 | #19 | z180 | 0 | 0° → −180° | — |
| 2 | #15 | z90 | 0 | −180° → −270° | — |
| 3 | #15 | x90, 100 µs | 0 | −270° → −270° | 90° |
| 4 | #15 | z90 | 100 | −270° → −360° | — |
| 5 | #13 | x90, 100 µs | 100 | −360° → −360° | 0° |
| 6 | #13 | z90 | 200 | −360° → −450° | — |
| 7 | #1 | x90, 100 µs | 200 | −450° → −450° | 270° |
| 8 | #19 recovery | z180 | 300 | −450° → −630° | — |

Here #15 inherits −180° from #19, so its RF axis is 90°. Starting #15 in isolation gave −90° in section 8.3.2. Check three RF pulses and five rz updates in the pulse sheet, and RF axes 90°, 0°, 270° in the waveform. XY uses nine RF pulses totaling 1400 µs. X/VZ uses three totaling 300 µs. These durations cover the RB control only, including recovery.

The final z180 leaves $F=-630^\circ$. Moving virtual Z factors to the left gives $U_{\rm logical}=R_z(\Theta)U_{\rm RF}$ with $\Theta=630^\circ=-F$. The recovered logical circuit is identity up to global phase. Trailing Z leaves a Z-basis population measurement invariant and remains relevant to subsequent phase-sensitive operations.
""")
md(r"""
### 8.6 Fit the synthetic example

For each site, calculate P0 per random sequence, average sequences equally at each depth and fit $A p^m+B$. Here B, fraction and p are bounded in [0,1] and A=(1−B)fraction, keeping the fitted curve in the probability range. The example reports $r_C=(1-p)/2$ without automatic calibration updates or confidence intervals. Its input is synthetic. For measured data examine residuals, drift, leakage and SPAM, check noise assumptions and resample entire sequences.
""")
code(
    r'''
def fit_rb_decay(depths, probabilities):
    """Fit the bounded teaching model A*p**m+B to synthetic data."""
    depths, probabilities = np.asarray(depths, dtype=float), np.asarray(probabilities, dtype=float)
    if (
        depths.ndim != 1 or probabilities.shape != depths.shape or len(depths) < 4
        or not np.isfinite(depths).all() or not np.isfinite(probabilities).all()
        or np.any(depths < 0) or len(np.unique(depths)) < 4
        or np.any((probabilities < 0) | (probabilities > 1))
    ):
        raise ValueError("Require finite probability data at >=4 distinct nonnegative depths")
    if np.ptp(probabilities) < 1e-10:
        raise ValueError("Constant data do not identify a decay rate")

    def residual(parameters):
        offset, fraction, p = parameters
        return offset + (1 - offset) * fraction * p**depths - probabilities

    candidates = [
        least_squares(residual, [min(probabilities) * .8, .8, p], bounds=(0, 1),
                      ftol=1e-12, xtol=1e-12, gtol=1e-12)
        for p in (.8, .95, .995)
    ]
    result = min(candidates, key=lambda item: np.sum(item.fun**2))
    offset, fraction, p = result.x
    return {"A": float((1 - offset) * fraction), "p": float(p), "B": float(offset),
            "residual_rms": float(np.sqrt(np.mean(result.fun**2))),
            "at_boundary": bool(np.any((result.x < 1e-6) | (result.x > 1 - 1e-6)))}
''',
    tags=("rb-definitions",),
)
code("""
# SYNTHETIC decay: independent of the compiled pulse program.
depths = np.array([0, 1, 4, 8, 16, 32, 64, 128])
rb_demo = .47 * .993**depths + .50
rb_fit = fit_rb_decay(depths, rb_demo)
print("Synthetic fit:", rb_fit, "; illustrative r_Clifford =", (1 - rb_fit["p"]) / 2)
""")
md(r"""
## 9. Clock spectroscopy, duration Rabi and state selectivity

Global RF π converts 00 to 11 and flips the spectator. Probe the selected $|1\rangle\leftrightarrow|c\rangle$ transition. Repeat with initial 0 while varying polarization, magnetic field, power and AOM frequency to check wrong-branch excitation. Use spectator Ramsey for off-zone light shifts and scattering. Resolve P(c), P(1) and loss. Spectroscopy exposes drift, sidebands and power broadening. Rabi scans probe Ω, return loss and branch mixing. The triggered sequence explicitly orders RF preparation before the optical probe.
""")
code("""
clock_frequencies = optical_frequency_axis(q0,"clock",[-5000,0,5000])
clock_probe = Square(duration=100*us,amplitude=.08,frequency=clock_frequencies)
clock_lengths = SweepParameter("clock_length",[25,50,100,150],"us")
clock_rabi_pulse = Square(duration=clock_lengths,amplitude=q0.parameters.clock.pi_amplitude)

def clock_scan(q, axis, pulse, rf_inversion, *, name, prepare_one=True, prepare, readout, shots=2):
    exp = Experiment(name,[q],metadata={"readout_required":"logical / c / loss"})
    with exp.average(shots), exp.sweep(axis), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            if prepare_one:
                play(q.rf,rf_inversion)
            play(q.clock,pulse)
        readout()
    return exp

clock_spec_preview = lab.compile(clock_scan(q0,clock_frequencies,clock_probe,rf_pi,name="clock_spectroscopy",**callbacks))
clock_rabi_preview = lab.compile(clock_scan(q0,clock_lengths,clock_rabi_pulse,rf_pi,name="clock_length_rabi",**callbacks))
clock_wrong_branch = clock_scan(q0,clock_frequencies,clock_probe,rf_pi,name="clock_wrong_branch",prepare_one=False,**callbacks)
""")
md(r"""
### 9.1 Clock chevrons and Ramsey

Prepare logical 1 with RF π, then drive the shared optical branch. Convert optical detuning using the measured signed AOM scale, including any double pass. Use P(c), P(1) and loss for Rabi transfer. Optical Ramsey combines atomic coherence, laser noise and motion. Both atoms see the same light. Repeat the wrong-spin control from section 9.
""")
code("""
clock_map_frequency = optical_frequency_axis(q0,"clock",[-2000,0,2000])
clock_map_time = SweepParameter("clock_map_time", [50,100,150], "us")
clock_dark_time = SweepParameter("clock_dark_time", [10,100,500], "us")
clock_map_pulse = Square(duration=clock_map_time,amplitude=q0.parameters.clock.pi_amplitude)

def clock_chevron(q, frequency, duration, probe, rf_inversion, *, prepare, readout, shots=2):
    exp = Experiment("clock_chevron_2d",[q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            play(q.clock,probe,frequency=frequency)
        readout()
    return exp

def clock_ramsey_map(q, frequency, dark_time, rf_inversion, half_pulse, *, prepare, readout, shots=2):
    exp = Experiment("clock_ramsey_frequency_time",[q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(dark_time), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            play(q.clock,half_pulse,frequency=frequency)
            delay(q.clock,dark_time)
            play(q.clock,half_pulse,frequency=frequency)
        readout()
    return exp

clock_chevron_preview = lab.compile(clock_chevron(q0,clock_map_frequency,clock_map_time,clock_map_pulse,rf_pi,**callbacks))
clock_ramsey_map_preview = lab.compile(clock_ramsey_map(q0,clock_map_frequency,clock_dark_time,rf_pi,clock_pi2,**callbacks))
""")
md(r"""
## 10. Optical Ramsey and nuclear-spin shelving phase

Clock π/2–τ–π/2 on $|1\rangle,|c\rangle$ measures optical frequency, coherence and axis phase. Convert electrical detuning through the AOM scale. To measure nuclear-spin round-trip phase, surround a clock π–hold–π pair with two RF half pulses. Scan the final RF phase against a duration- and trap-matched dark reference. Even perfect equal-phase clock π pulses give $|1\rangle$ a minus sign on return. Population return alone cannot test nuclear-spin identity.

The round-trip phase includes the optical phase difference, clock/Rydberg light shifts, hold phase and transport history. Independent DSL frames cannot infer it. Hold RF/clock references fixed and vary the return phase, hold time and pre-shelving wait. Measure the sign and slope of the necessary correction.
""")
code("""
def optical_ramsey(q,tau,rf_inversion,pi2_pulse,*,optical_detuning_hz=1000.,prepare,readout,shots=2):
    frequency = (q.parameters.clock.frequency+optical_detuning_hz/optical_mapping["clock"]["frequency_scale"])*Hz
    exp = Experiment("clock_optical_ramsey",[q])
    with exp.average(shots),exp.sweep(tau),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            play(q.clock,pi2_pulse,frequency=frequency)
            delay(q.clock,tau)
            play(q.clock,pi2_pulse,frequency=frequency)
        readout()
    return exp

def clock_roundtrip_phase(q,phase,rf_half,clock_inversion,*,hold=10*us,cycles=1,light_on=True,
                          return_phase=0*rad,prepare,readout,shots=2):
    exp = Experiment("clock_roundtrip_phase" if light_on else "matched_idle_phase",[q],
                     metadata={"clock_cycles":cycles,"light_on":light_on})
    with exp.average(shots),exp.sweep(phase),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_half)
            for _ in range(cycles):
                if light_on:
                    play(q.clock,clock_inversion)
                    delay(q.clock,hold)
                    play(q.clock,clock_inversion,phase=return_phase)
                else:
                    delay(q.rf,2*q.parameters.clock.pi_duration+float(hold))
            play(q.rf,rf_half,phase=phase)
        readout()
    return exp

clock_tau = SweepParameter("clock_tau",[10,100,500],"us")
phase_axis = SweepParameter("analysis_phase",[0,90,180,270],"deg")
clock_optical_preview = lab.compile(optical_ramsey(q0,clock_tau,rf_pi,clock_pi2,**callbacks))
clock_phase_preview = lab.compile(clock_roundtrip_phase(q0,phase_axis,rf_pi2,clock_pi,**callbacks))
idle_phase_preview = lab.compile(clock_roundtrip_phase(q0,phase_axis,rf_pi2,clock_pi,light_on=False,**callbacks))

phi = np.linspace(0,2*np.pi,24,endpoint=False)
fringe_reference = .5+.45*np.cos(phi+.12)
fringe_clock = .5+.42*np.cos(phi+.12+.37)
fit_reference = analysis.fit_fringe(phi,fringe_reference)
fit_clock = analysis.fit_fringe(phi,fringe_clock)
phase_shift = np.angle(np.exp(1j*(fit_clock["phase_rad"]-fit_reference["phase_rad"])))
print("SYNTHETIC round-trip fringe shift (rad):",phase_shift)
""")
md(r"""
### 10.1 Shelving hold versus RF analysis phase

Compare nuclear-spin fringes at each hold with a light-off control that retains both clock pulse slots and the full hold. Extract phase shift and contrast. Interpret phase divided by hold as a frequency shift only where the response is linear.
""")
code("""
shelving_holds = SweepParameter("shelving_hold", [10,100,500], "us")
shelving_phases = SweepParameter("shelving_analysis_phase", [0,90,180,270], "deg")

def shelving_phase_map(q, hold, phase, rf_half, clock_inversion, *, light_on, return_phase=0*rad, prepare, readout, shots=2):
    exp = Experiment(f"shelving_phase_map_{light_on}",[q])
    with exp.average(shots), exp.sweep(hold), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_half)
            if light_on:
                play(q.clock,clock_inversion)
                delay(q.clock,hold)
                play(q.clock,clock_inversion,phase=return_phase)
            else:
                delay(q.rf,clock_inversion.duration)
                delay(q.rf,hold)
                delay(q.rf,clock_inversion.duration)
            play(q.rf,rf_half,phase=phase)
        readout()
    return exp

shelving_map_preview = lab.compile(shelving_phase_map(q0,shelving_holds,shelving_phases,rf_pi2,clock_pi,light_on=True,**callbacks))
shelving_idle_preview = lab.compile(shelving_phase_map(q0,shelving_holds,shelving_phases,rf_pi2,clock_pi,light_on=False,**callbacks))
""")
md(r"""
### 10.2 Differential light-shift Ramsey

Use RF π/2, an off-resonant clock probe and RF analysis. Scan probe amplitude and analysis phase. A zero-amplitude pulse of equal length gives the reference. Verify extinction and detuning from every relevant branch. Record phase, contrast, residual c population, loss and spectator readout. Measure stray illumination at storage sites. Adding a numerical phase in the DSL would bypass the physical light shift under test.
""")
code("""
stark_amplitudes = SweepParameter("clock_probe_amplitude", [0,.04,.08], "dimensionless")
stark_phases = SweepParameter("stark_analysis_phase", [0,90,180,270], "deg")
stark_frequency = (q0.parameters.clock.frequency + 50000/optical_mapping["clock"]["frequency_scale"])*Hz
stark_probe = Square(duration=100*us,amplitude=stark_amplitudes,frequency=stark_frequency)

def clock_light_shift(q, amplitude, phase, half_pulse, probe, *, prepare, readout, shots=2):
    exp = Experiment("clock_light_shift_ramsey",[q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,half_pulse)
            play(q.clock,probe)
            play(q.rf,half_pulse,phase=phase)
        readout()
    return exp

stark_preview = lab.compile(clock_light_shift(q0,stark_amplitudes,stark_phases,rf_pi2,stark_probe,**callbacks))
""")
md(r"""
## 11. Clock population return, coherent return and optical RB

1. Start in 1 and repeat complete π–hold–π cycles for n=0,1,2,4,8,… Record logical return, residual clock population and survival. Repeat with initial 0. Coherent population oscillations may preclude a simple exponential.
2. Wrap the repeated cycles in RF Ramsey, measure phase slope and contrast decay, and infer the effect on nuclear-spin coherence.
3. If you control clock pulse phase and resolve both optical states, run 24-Clifford RB in $\{|1\rangle,|c\rangle\}$. It characterizes optical Cliffords. Assess the shelving pair in a nuclear-spin CZ separately, with cooling, trapping and optics matched to the intended gate.
""")
code("""
clock_gate_map = {name:(clock_pi if name in ("x180","y180") else clock_pi2,phase)
                  for name,(_,phase) in rf_gate_map.items() if name in ("x90","xm90","y90","ym90","x180","y180")}

def clock_return_train(q,rf_inversion,clock_inversion,*,cycles,hold=10*us,prepare,readout,shots=2):
    exp = Experiment("clock_population_return",[q],metadata={"cycles":cycles})
    with exp.average(shots),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            for _ in range(cycles):
                play(q.clock,clock_inversion)
                delay(q.clock,hold)
                play(q.clock,clock_inversion)
        readout()
    return exp

def clock_rb(q,sequence,rf_inversion,gate_map,*,prepare,readout,shots=2):
    exp = Experiment("clock_optical_rb",[q],metadata={"rb":sequence,"encoding":"1,c optical subspace"})
    with exp.average(shots),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            for gate_name in sequence["native_word"]:
                pulse,phase = gate_map[gate_name]
                play(q.clock,pulse,phase=(phase/optical_mapping["clock"]["phase_scale"])*deg)
        readout()
    return exp

return_train_preview = lab.compile(clock_return_train(q0,rf_pi,clock_pi,cycles=2,**callbacks))
# Expand the same logical circuit in reference XY order for the optical frame.
# RF virtual Z belongs to the spin frame. Preserve the optical pulse order here.
clock_rb_logical = next(c for c in rb_logical_circuits if c["length"] == 4 and c["sequence"] == 0)
clock_rb_short = {
    **clock_rb_logical,
    "basis": "fixed_xy_reference",
    "native_word": [
        name
        for index in [*clock_rb_logical["clifford_indices"], clock_rb_logical["recovery_index"]]
        for name in rb_catalog[index]["reference_word"]
    ],
}
clock_rb_preview = lab.compile(clock_rb(q0,clock_rb_short,rf_pi,clock_gate_map,**callbacks))
""")
md(r"""
### 11.1 Matched clock hold and residual-state controls

Compare shelve/return, shelve without return and no shelving. Keep both optical pulse slots with duration-matched delays when omitted. Measure residual c, returned 1, other ground states and true loss. Separating these channels is necessary for a metastable lifetime estimate. Return alone also includes transfer and detection errors.
""")
code("""
def clock_population_hold(q, hold, rf_inversion, clock_inversion, *, shelve=True, return_to_spin=True, prepare, readout, shots=2):
    exp = Experiment(f"clock_hold_{shelve}_{return_to_spin}",[q])
    with exp.average(shots), exp.sweep(hold), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            if shelve:
                play(q.clock,clock_inversion)
            else:
                delay(q.clock,clock_inversion.duration)
            delay(q.clock,hold)
            if shelve and return_to_spin:
                play(q.clock,clock_inversion)
            else:
                delay(q.clock,clock_inversion.duration)
        readout()
    return exp

clock_hold_return_preview = lab.compile(clock_population_hold(q0,shelving_holds,rf_pi,clock_pi,**callbacks))
clock_hold_residual_preview = lab.compile(clock_population_hold(q0,shelving_holds,rf_pi,clock_pi,return_to_spin=False,**callbacks))
clock_hold_idle_preview = lab.compile(clock_population_hold(q0,shelving_holds,rf_pi,clock_pi,shelve=False,**callbacks))
""")
md(r"""
## 12. Rydberg spectroscopy, Rabi and blockade controls

Prepare 1, shelf to c, apply the UV probe and perform state-selective detection or recapture. First calibrate the bare transition and Ω with a singly occupied zone or widely separated atoms. Then check collective enhancement and suppression of double excitation at the target pair spacing. The q0/q1 preview addresses both atoms. Single-atom controls require actual single occupancy or transport of the other atom, with matching configuration and recorded occupancy. Changing a software target cannot move an illuminated atom.

Examine Zeeman and stray levels, light shifts and ionization during frequency scans, and decay and recapture during time scans. Report P(rr), P(cr)+P(rc), P(cc) and loss for blockade measurements. Inferring Rydberg population from loss requires independent recovery, de-excitation or field-ionization calibration. Scan distance, power and hold time to assess $V/\Omega$ and lifetime.
""")
code("""
rydberg_frequencies = optical_frequency_axis(q0,"rydberg",[-100000,0,100000])
rydberg_probe = Square(duration=2*us,amplitude=.08,frequency=rydberg_frequencies)
rydberg_lengths = SweepParameter("rydberg_length",[.5,1.,2.,3.],"us")
rydberg_rabi_pulse = Square(duration=rydberg_lengths,amplitude=q0.parameters.rydberg.pi_amplitude)

def rydberg_scan(q,axis,probe,rf_inversion,clock_inversion,*,name,prepare,readout,shots=2):
    exp = Experiment(name,[q],metadata={"zone_occupancy":"two atoms; interacting pair", "readout_required":"c/r/ground/loss discrimination"})
    with exp.average(shots),exp.sweep(axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play(q.clock,clock_inversion)
            play(q.rydberg,probe)
        readout()
    return exp

rydberg_spec_preview = lab.compile(rydberg_scan(q0,rydberg_frequencies,rydberg_probe,rf_pi,clock_pi,name="pair_rydberg_spectrum",**callbacks))
rydberg_rabi_preview = lab.compile(rydberg_scan(q0,rydberg_lengths,rydberg_rabi_pulse,rf_pi,clock_pi,name="pair_rydberg_rabi",**callbacks))
""")
md(r"""
### 12.1 Pair Rydberg chevron and return/hold diagnostic

The UV chevron ends in the c/r manifold like the 1D probe. Acquire physically prepared A-only, B-only and pair controls with occupancy recorded. Analyze pair oscillations together with suppressed Prr. [Gaëtan et al.](https://arxiv.org/abs/0810.2960)

A separate UV π–hold–π diagnostic returns to ground via the clock pulse. The nominal UV π is calibrated on one atom. Pair coupling changes the transfer. Decay branches, re-excitation, trap switching and recapture all affect the signal. The fixture has no trap control and the notebook does not extract a Rydberg lifetime.
""")
code("""
rydberg_map_frequency = optical_frequency_axis(q0,"rydberg",[-100000,0,100000])
rydberg_map_time = SweepParameter("rydberg_map_time", [1,2,3], "us")
rydberg_map_pulse = Square(duration=rydberg_map_time,amplitude=q0.parameters.rydberg.pi_amplitude)
rydberg_holds = SweepParameter("rydberg_hold", [1,5,10], "us")

def rydberg_chevron(q, frequency, duration, probe, rf_inversion, clock_inversion, *, prepare, readout, shots=2):
    exp = Experiment("pair_rydberg_chevron_2d",[q],metadata={"occupancy":"interacting pair; requires joint c/r/ground/loss readout"})
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play(q.clock,clock_inversion)
            play(q.rydberg,probe,frequency=frequency)
        readout()
    return exp

def rydberg_return_hold(q, hold, rf_inversion, clock_inversion, uv_inversion, *, prepare, readout, shots=2):
    exp = Experiment("pair_rydberg_return_hold",[q],metadata={"interpretation":"return diagnostic, not isolated Rydberg T1"})
    with exp.average(shots), exp.sweep(hold), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play(q.clock,clock_inversion)
            play(q.rydberg,uv_inversion)
            delay(q.rydberg,hold)
            play(q.rydberg,uv_inversion)
            play(q.clock,clock_inversion)
        readout()
    return exp

rydberg_chevron_preview = lab.compile(rydberg_chevron(q0,rydberg_map_frequency,rydberg_map_time,rydberg_map_pulse,rf_pi,clock_pi,**callbacks))
rydberg_return_preview = lab.compile(rydberg_return_hold(q0,rydberg_holds,rf_pi,clock_pi,rydberg_pi,**callbacks))
""")
md(r"""
## 13. A complete CZ with two zone-wide blockade pulses

Use two equal-duration, equally detuned UV pulses and shift the second phase. Only logical 1 is shelved, so the single-excitation and collective cc branches acquire different phases. Global illumination supports this sequence without separate control/target addressing.

The model uses $R_\phi(\theta)=\exp[-i\theta(\cos\phi X+\sin\phi Y)/2]$ and $H/\Omega=\begin{pmatrix}0&e^{-i\phi}/2\\e^{i\phi}/2&-\Delta/\Omega\end{pmatrix}$. Ideal blockade gives collective coupling $\sqrt2\Omega$. Solve closure and conditional phase π for a dimensionless starting point. The second-pulse phase sign depends on the chosen convention and measured AOM mapping.

The model omits motion, decay, light shifts, phase noise and hardware response. `logical_diag` includes the ideal single-atom sign from shelving and return. The model's common phase is a placeholder for an atomic calibration.
""")
code("""
cz_seed = analysis.solve_collective_cz_seed()
cz_model = analysis.collective_cz_model(**cz_seed)
omega_rad_s = np.pi/q0.parameters.rydberg.pi_duration
duration_ideal_s = cz_seed["duration_omega"]/omega_rad_s
# Quantize demo duration to 8 ns; keep both ideal seed and compiled setting.
uv_duration_s = round(duration_ideal_s/8e-9)*8e-9
uv_detuning_hz = cz_seed["delta_over_omega"]*omega_rad_s/(2*np.pi)
uv_drive_frequency = (q0.parameters.rydberg.frequency + uv_detuning_hz/optical_mapping["rydberg"]["frequency_scale"])*Hz
uv_phase = (cz_seed["phase_rad"]/optical_mapping["rydberg"]["phase_scale"])*rad
clock_return_phase = (cz_model["clock_return_phase_rad"]/optical_mapping["clock"]["phase_scale"])*rad
uv_gate_pulse = Square(duration=uv_duration_s,amplitude=q0.parameters.rydberg.pi_amplitude)
CZ_GAP = 2*us
GATE_TIME_S = 2*q0.parameters.clock.pi_duration + 2*float(CZ_GAP) + 2*uv_duration_s

print("Ideal blockade seed:",cz_seed)
print("Ideal return:",cz_model["single_return"],cz_model["pair_return"])
print("Ideal / quantized UV duration (us):",duration_ideal_s*1e6,uv_duration_s*1e6)
print("Model-only outgoing clock phase (rad):",float(clock_return_phase))
quantized_model = analysis.collective_cz_model(cz_seed["delta_over_omega"],uv_duration_s*omega_rad_s,cz_seed["phase_rad"])
print("Quantized seed model return:",quantized_model["single_return"],quantized_model["pair_return"])

def play_full_gate(q,clock_inversion,uv_pulse,*,frequency,second_phase,return_phase,variant="cz"):
    # Ordinary Python authoring helper: appends plain DSL events, never hardware I/O.
    # Every 'cz' occurrence below contains a complete clock up/down cycle.
    if variant == "idle":
        delay(q.rf,GATE_TIME_S)
    elif variant == "clock":
        play(q.clock,clock_inversion)
        delay(q.clock,2*float(CZ_GAP)+2*uv_duration_s)
        # Same-phase ideal roundtrip is Z on each atom, not logical identity.
        play(q.clock,clock_inversion)
    elif variant == "cz":
        play(q.clock,clock_inversion)
        delay(q.clock,CZ_GAP)
        play(q.rydberg,uv_pulse,frequency=frequency)
        play(q.rydberg,uv_pulse,frequency=frequency,phase=second_phase)
        delay(q.clock,CZ_GAP)
        play(q.clock,clock_inversion,phase=return_phase)
    else:
        raise ValueError("variant must be idle, clock or cz")

gate_controls = {"frequency":uv_drive_frequency,"second_phase":uv_phase,"return_phase":clock_return_phase}
""")
md(r"""
### 13.1 Tune closure before phase

Scan Ω, Δ, pulse duration and second-pulse phase. Use single-atom and pair return/leakage to narrow the candidates before echo phase optimization. Fix the other dimensions on the host while scanning a small grid. The model below illustrates how finite $V/\Omega$ shifts the ideal seed. Recalibrate in situ for trap switching, finite UV edges, inter-pulse gaps and variation in pair spacing and Ω.
""")
code("""
blockade_ratios = np.array([5,8,12,20,40,80,160],dtype=float)
finite_models = [analysis.collective_cz_model(**cz_seed,blockade_over_omega=b) for b in blockade_ratios]
fig, axes = plt.subplots(1,2,figsize=(8,3))
axes[0].semilogx(blockade_ratios,[1-m["pair_return"] for m in finite_models],"o-")
axes[0].set(xlabel="V / Omega",ylabel="1 - pair return",title="MODEL leakage")
axes[1].semilogx(blockade_ratios,[np.angle(np.exp(1j*(m["conditional_phase_rad"]-np.pi))) for m in finite_models],"o-")
axes[1].set(xlabel="V / Omega",ylabel="conditional phase - pi (rad)",title="MODEL phase error")
fig.savefig(OUTPUT / "model_finite_blockade.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "model_finite_blockade.png")))
""")
md(r"""
### 13.2 Two-pulse closure versus duration and frequency

Prepare 11, apply clock up → two UV pulses → clock down, then measure P11, other logical populations, residual excitation and loss. Identity can also close population. Qualify candidates with χ and common phase next. Both UV pulses vary together at fixed second phase. Recompute matched reference time when duration changes.
""")
code("""
cz_closure_frequency = SweepParameter("cz_closure_hz", [float(uv_drive_frequency)+d for d in (-10000,0,10000)], "Hz")
cz_closure_duration = SweepParameter("cz_uv_duration", [uv_duration_s+d for d in (-80e-9,0,80e-9)], "s")
cz_closure_pulse = Square(duration=cz_closure_duration,amplitude=q0.parameters.rydberg.pi_amplitude)

def cz_closure_map(q, frequency, duration, rf_inversion, clock_inversion, uv_pulse, *, second_phase, return_phase, prepare, readout, shots=2):
    exp = Experiment("cz_two_pulse_closure_2d",[q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
        readout()
    return exp

cz_closure_preview = lab.compile(cz_closure_map(q0,cz_closure_frequency,cz_closure_duration,rf_pi,clock_pi,cz_closure_pulse,second_phase=uv_phase,return_phase=clock_return_phase,**callbacks))
""")
md(r"""
## 14. Calibrate entangling phase χ with global echo

Approximate the gate as $G\doteq\mathrm{diag}(1,e^{i\delta_B},e^{i\delta_A},e^{i(\delta_A+\delta_B+\chi)})$. Apply Yπ/2 → Gᴹ → Xπ → Gᴹ → Xπ → −Yπ/2 → readout, broadcasting all RF. Static local phases cancel ideally, giving $P_{11}=\sin^2(M\chi/2)$ and $P_{01}=P_{10}=0$. Start at M=1, then use odd M such as 3 or 5 to amplify errors. Scan both sides of an extremum to resolve the correct branch.

Each sequence contains 2M complete ground-state gates. Echo leaves individual-gate common and differential local phases underconstrained. Noise, off-diagonal errors and leakage may persist. Constrain logical survival, clock residuals and loss when optimizing P11.
""")
code("""
def cz_echo_phase(q,uv_phase_axis,rf_half,rf_inversion,clock_inversion,uv_pulse,*,
                  frequency,return_phase,M=1,prepare,readout,shots=2):
    exp = Experiment("cz_global_echo_phase",[q],metadata={"complete_cz_count":2*M,"M":M})
    with exp.average(shots),exp.sweep(uv_phase_axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half,phase=90*deg)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,
                               second_phase=uv_phase_axis,return_phase=return_phase)
            play(q.rf,rf_inversion)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,
                               second_phase=uv_phase_axis,return_phase=return_phase)
            play(q.rf,rf_inversion)
            play(q.rf,rf_half,phase=270*deg)
        readout()
    return exp

uv_phase_axis = SweepParameter("uv_second_phase",[float(uv_phase)+d for d in (-.08,0,.08)],"rad")
cz_echo_preview = lab.compile(cz_echo_phase(q0,uv_phase_axis,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,
                        frequency=uv_drive_frequency,return_phase=clock_return_phase,**callbacks))
cz_echo_preview.export_report(OUTPUT / "cz_echo_report")

def collective_rotation(theta,phase):
    r = analysis.rotation(theta,phase)
    return np.kron(r,r)

ket00 = np.array([1,0,0,0],complex)
Xg = collective_rotation(np.pi,0)
for chi in (.3,np.pi,3.4):
    D = np.diag(np.exp(1j*np.array([0,.31,-.24,.31-.24+chi])))
    E = Xg@D@Xg@D
    state = collective_rotation(-np.pi/2,np.pi/2)@E@collective_rotation(np.pi/2,np.pi/2)@ket00
    np.testing.assert_allclose(abs(state)**2,[np.cos(chi/2)**2,0,0,np.sin(chi/2)**2],atol=1e-12)
print("Independent ideal matrix check: echo cancels static local phases.")
""")
md(r"""
### 14.1 Echo versus UV second phase and frequency

Scan both settings inside the closure-qualified region. Use M=1 for initial tuning and M=3 for amplification, with M complete gates per echo arm. Keep P01, P10 and loss alongside P11. Only the small M=1 grid is compiled here.
""")
code("""
def cz_echo_map(q, frequency, phase, rf_half, rf_inversion, clock_inversion, uv_pulse, *, return_phase, M=1, prepare, readout, shots=2):
    exp = Experiment("cz_echo_phase_frequency",[q],metadata={"complete_cz_count":2*M})
    with exp.average(shots), exp.sweep(frequency), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half,phase=90*deg)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=phase,return_phase=return_phase)
            play(q.rf,rf_inversion)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=phase,return_phase=return_phase)
            play(q.rf,rf_inversion)
            play(q.rf,rf_half,phase=270*deg)
        readout()
    return exp

cz_echo_map_preview = lab.compile(cz_echo_map(q0,cz_closure_frequency,uv_phase_axis,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,return_phase=clock_return_phase,**callbacks))
""")
md(r"""
## 15. Bell populations, common phase and parity

Assume similar local phases on the two zone atoms. With $G_c=\mathrm{CZ}$, Xπ/2 → Gc → −Yπ/2 on 00 prepares $(|00\rangle+i|11\rangle)/\sqrt2$ and target Bell phase $\beta_0=\pi/2$. First scan a common compensation to minimize odd population. Here the clock return pulse phase supplies the optical knob. A calibrated RF-frame alternative affects all subsequent global RF, including storage spectators. Apply one compensation convention.

Fix the compensation and scan phase ϑ of a common analysis π/2. Fit $\Pi=P_{00}+P_{11}-P_{01}-P_{10}=\Pi_0-C\cos(2\vartheta-\beta)$. Fidelity to the specified target is $[P_{00}+P_{11}+C\cos(\beta-\beta_0)]/2$. Amplitude alone misses deterministic phase errors; even population alone has a π ambiguity. This metric characterizes the prepared Bell output. A common correction cannot remove arbitrary $\delta_A-\delta_B$. If phase and populations disagree, check zone intensity, light shifts, RF nonuniformity, differential phase and leakage.
""")
code("""
def bell_population(q,return_phase_axis,rf_half,clock_inversion,uv_pulse,*,frequency,second_phase,prepare,readout,shots=2):
    exp = Experiment("bell_common_phase",[q])
    with exp.average(shots),exp.sweep(return_phase_axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half)
            play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase_axis)
            play(q.rf,rf_half,phase=270*deg)
        readout()
    return exp

def bell_parity(q,phase_axis,rf_half,clock_inversion,uv_pulse,*,frequency,second_phase,return_phase,prepare,readout,shots=2):
    exp = Experiment("bell_parity",[q])
    with exp.average(shots),exp.sweep(phase_axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half)
            play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
            play(q.rf,rf_half,phase=270*deg)
            play(q.rf,rf_half,phase=phase_axis)
        readout()
    return exp

return_phase_axis = SweepParameter("clock_return_phase",[float(clock_return_phase)+d for d in (-.2,0,.2)],"rad")
population_preview = lab.compile(bell_population(q0,return_phase_axis,rf_pi2,clock_pi,uv_gate_pulse,
                            frequency=uv_drive_frequency,second_phase=uv_phase,**callbacks))
parity_axis = SweepParameter("parity_phase",[0,45,90,135],"deg")
parity_preview = lab.compile(bell_parity(q0,parity_axis,rf_pi2,clock_pi,uv_gate_pulse,**gate_controls,**callbacks))

CZ = np.diag([1,1,1,-1])
bell = collective_rotation(np.pi/2,-np.pi/2)@CZ@collective_rotation(np.pi/2,0)@ket00
target_bell = np.array([1,0,0,1j])/np.sqrt(2)
assert np.isclose(abs(np.vdot(target_bell,bell))**2,1)
theta = np.linspace(0,2*np.pi,24,endpoint=False)
ideal_parity = np.array([np.dot([1,-1,-1,1],abs(collective_rotation(np.pi/2,t)@bell)**2) for t in theta])
np.testing.assert_allclose(ideal_parity,-np.cos(2*theta-np.pi/2),atol=1e-12)

# SYNTHETIC parity; fit both quadratures, not contrast alone.
parity_demo = .02-.86*np.cos(2*theta-(np.pi/2+.12))
design = np.column_stack([np.ones_like(theta),np.cos(2*theta),np.sin(2*theta)])
offset,b,c = np.linalg.lstsq(design,parity_demo,rcond=None)[0]
contrast = np.hypot(b,c)
beta = np.arctan2(-c,-b)
even_population_demo = .94
F_target_demo = (even_population_demo+contrast*np.cos(beta-np.pi/2))/2
print("SYNTHETIC target-phase Bell F:",F_target_demo,"; fitted beta:",beta)
fig,ax = plt.subplots(figsize=(7,3))
ax.plot(theta*180/np.pi,parity_demo,"o",label="Synthetic parity")
ax.plot(theta*180/np.pi,design@[offset,b,c],label="2-phase fit")
ax.set(xlabel="Global analysis phase (deg)",ylabel="Parity",title="SYNTHETIC Bell parity")
ax.legend()
fig.savefig(OUTPUT / "synthetic_bell_parity.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "synthetic_bell_parity.png")))
""")
md(r"""
### Optional extension. Identify phases within a diagonal model

With four site-resolved outcomes, common $R_y(\theta)$ preparation, G and common X/Y analyses can identify phases within a chosen diagonal model without independent 01/10 preparation. Use $\theta=\pi/3,2\pi/3$ and cross-check at π/2. For $z_i=\langle X_i\rangle+i\langle Y_i\rangle$, fit $z_i/\sin\theta=A_i+B_i\sin^2(\theta/2)$ where $A_i=e^{i\delta_i}$ and $B_i=A_i(e^{i\chi}-1)$. Compare χ from both sites. This derivation assumes calibrated RF/SPAM and diagonal action. Parity alone, pooled P01+P10 or just |++⟩ at ideal χ=π can lose identifiability. Use echo and Bell for the primary protocol.
""")
md(r"""
### 15.1 Repeated complete CZ gates and input-dependent loss

First apply global Yπ/2 → Gⁿ → global analysis π/2 while scanning n and analysis phase. n=0 gives the reference and even/odd n resolve periodicity and accumulated phase. Second, prepare 00, ++ or 11, repeat G and omit the analysis pulse. Measure logical survival, c/r residuals and loss versus n. This adapts an input-state comparison to ground-spin qubits without mid-circuit detection. Final loss cannot localize an erasure to an individual gate. [Ma et al.](https://arxiv.org/abs/2305.05493)
""")
code("""
cz_repeat_counts = SweepParameter("complete_cz_repeats", [0,1,2], "dimensionless")
cz_repeat_phases = SweepParameter("cz_repeat_analysis_phase", [0,90,180,270], "deg")

def cz_repetition_map(q, count, phase, rf_half, clock_inversion, uv_pulse, *, frequency, second_phase, return_phase, prepare, readout, shots=2):
    exp = Experiment("cz_coherent_repetition",[q])
    with exp.average(shots), exp.sweep(count), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half,phase=90*deg)
            with exp.repeat(count):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
            play(q.rf,rf_half,phase=phase)
        readout()
    return exp

def cz_input_loss_scan(q, count, rf_half, rf_inversion, clock_inversion, uv_pulse, *, input_state, frequency, second_phase, return_phase, prepare, readout, shots=2):
    if input_state not in ("00","++","11"):
        raise ValueError("input_state must be 00, ++ or 11")
    exp = Experiment(f"cz_input_loss_{input_state}",[q],metadata={"input_state":input_state,"readout":"joint logical/c/r/loss, no final RF analysis"})
    with exp.average(shots), exp.sweep(count), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            # Equal-length preparation slot for all three input states.
            if input_state == "11":
                play(q.rf,rf_inversion)
            elif input_state == "++":
                play(q.rf,rf_half,phase=90*deg)
                delay(q.rf,float(rf_inversion.duration)-float(rf_half.duration))
            else:
                delay(q.rf,rf_inversion.duration)
            with exp.repeat(count):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
        readout()
    return exp

cz_repetition_preview = lab.compile(cz_repetition_map(q0,cz_repeat_counts,cz_repeat_phases,rf_pi2,clock_pi,uv_gate_pulse,**gate_controls,**callbacks))
cz_loss_zero_preview = lab.compile(cz_input_loss_scan(q0,cz_repeat_counts,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,input_state="00",**gate_controls,**callbacks))
cz_loss_plus_preview = lab.compile(cz_input_loss_scan(q0,cz_repeat_counts,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,input_state="++",**gate_controls,**callbacks))
cz_loss_one_preview = lab.compile(cz_input_loss_scan(q0,cz_repeat_counts,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,input_state="11",**gate_controls,**callbacks))
""")
md(r"""
## 16. Randomized global echo for RF, clock and full gates

Each block applies Ri → G → Xπg → G. For ideal CZ, CZ·Xg·CZ ≃ Yg, allowing a recovery using global RF alone. Compute separate recoveries for matched idle and clock-only controls because their ideal blocks differ. The clock-only pair ideally implements Z on each atom. Match control waits to the complete gate duration.

This GERB-style diagnostic uses random common Clifford half pulses. The published U-GERB protocol uses Haar rotations. Share the RF seed across conditions and count two G operations per block. $A p^{N_{\rm block}}+B$ describes block decay. A square root, p itself or a full-two-qubit depolarizing conversion cannot identify CZ fidelity here. Echo may hide repeatable local phases. Use the following echo-free test as the main CZ benchmark.
""")
code("""
def global_gate_word(q,word,gate_map,clock_inversion,uv_pulse,*,name,variant="cz",
                     frequency,second_phase,return_phase,prepare,readout,shots=2):
    exp = Experiment(name,[q],metadata={"word":list(word),"variant":variant,
                                       "complete_gate_count":sum(g in ("G","cz") for g in word)})
    with exp.average(shots),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            for gate in word:
                if gate in ("G","cz"):
                    play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,
                                   return_phase=return_phase,variant=variant)
                else:
                    pulse,phase = gate_map[gate]
                    play(q.rf,pulse,phase=phase*deg)
        readout()
    return exp

echo_benchmark_previews = []
echo_words = {variant:analysis.global_echo_word(2,seed=171,variant=variant) for variant in ("idle","clock","cz")}
for variant,word in echo_words.items():
    unitary = analysis.global_echo_unitary(word,variant=variant)
    assert np.isclose(abs(unitary[0,0])**2,1)
    print(variant,word)
    echo_benchmark_preview = lab.compile(global_gate_word(q0,word,rf_gate_map,clock_pi,uv_gate_pulse,
                                     name=f"global_echo_random_{variant}",variant=variant,**gate_controls,**callbacks))
    echo_benchmark_previews.append(echo_benchmark_preview)
(OUTPUT/"echo_random_words.json").write_text(json.dumps(echo_words,indent=2),encoding="utf-8")
""")
md(r"""
## 17. Echo-free symmetric stabilizer benchmarking

Global X90, −X90, Y90 and −Y90 rotations with complete CZ generate 12 symmetric stabilizer states from 00. Enumerate them explicitly. [Tsai et al.](https://doi.org/10.1103/PRXQuantum.6.010331)

| Stage | Fixed template | Counts |
|---|---|---|
| Initialize | R1 → R2 → CZ → R3 → R4 → R5 | 5 RF, 1 CZ |
| Middle | L random RF pulses. Insert one CZ after each of the first n−2 pulses | L RF, n−2 CZ |
| Recover | R1 → R2 → CZ → R3 → R4 | 4 RF, 1 CZ |

Every circuit uses L+9 RF half pulses and n complete CZ operations, initialization and recovery included. Enumerate 4⁵ initialization words and 4⁴ recovery words, cover all 12 states and verify return to 00. Balance the initial states at every depth with independent random words. Ideal gates permute the set and preserve its uniform distribution. The template follows Fig. 2 and Appendix E of the ⁸⁸Sr optical-qubit paper. Here RF drives ¹⁷¹Yb, each CZ includes the clock round trip and recovery targets 00 (the publication uses 11). This samples the symmetric stabilizer set. Interleave depths and record pair, seed, initial state, sequence and shot. Freeze tuning before validating a new seed.
""")
code("""
ssb_records = analysis.ssb_sequences(total_cz_counts=(2,4,6,10,14,18,26),random_rf_count=24,repeats_per_state=2,seed=171)
assert all(r["rf_count"]==33 and r["word"].count("cz")==r["total_cz_count"] for r in ssb_records)
for record in ssb_records:
    U = analysis.global_word_unitary(record["word"])
    assert np.isclose(abs(U[0,0])**2,1)
for n in (2,4,6,10,14,18,26):
    ids = [r["initial_state"] for r in ssb_records if r["total_cz_count"]==n]
    assert len(set(ids))==12 and all(ids.count(i)==2 for i in set(ids))
(OUTPUT/"ssb_sequences.json").write_text(json.dumps(ssb_records,indent=2),encoding="utf-8")
print("Frozen SSB sequences:",len(ssb_records),"; all ideal returns verified; only one compiled below.")
ssb_record = ssb_records[0]
ssb_preview = lab.compile(global_gate_word(q0,ssb_record["word"],rf_gate_map,clock_pi,uv_gate_pulse,
                      name="ssb_representative",**gate_controls,**callbacks))
""")
md(r"""
### 17.1 Fit SSB and report its scope

In a short, small-error range fit $P_{\rm return}(n)=A F_{\rm SSB}^n$. This is a protocol-inferred symmetric-subspace metric. Check the error model and depth stability before interpreting it. Avoid arbitrary fixed backgrounds or a full-two-qubit depolarizing conversion.

Compare unconditional return, return conditional on logical survival, atom survival and leakage. Drift, gate-dependent error, returned leakage and coherent oscillation can break a single exponential. Vary depth range, check residuals, use independent seeds and resample sequences stratified by initial state. Repeated shots of one circuit share its random word.

The principal subspace is $\mathrm{span}\{|00\rangle,(|01\rangle+|10\rangle)/\sqrt2,|11\rangle\}$. The singlet and coherences across subspaces receive incomplete coverage. Separate local control would enable additional full-2Q reference/interleaved RB.
""")
code("""
ssb_depths = np.array([2,4,6,10,14,18,26])
ssb_demo = .96*.992**ssb_depths
ssb_fit = analysis.fit_ssb_short(ssb_depths,ssb_demo)
print("SYNTHETIC short-depth protocol fit:",ssb_fit)
fig,axes = plt.subplots(1,2,figsize=(8,3))
pred = ssb_fit["A"]*ssb_fit["F_ssb"]**ssb_depths
axes[0].plot(ssb_depths,ssb_demo,"o",label="Synthetic return")
axes[0].plot(ssb_depths,pred,label="A F_ssb^n")
axes[0].set(xlabel="Complete CZ count (incl. init/recovery)",ylabel="P00",title="SYNTHETIC SSB")
axes[0].legend()
axes[1].plot(ssb_depths,ssb_demo-pred,"o-")
axes[1].set(xlabel="Complete CZ count",ylabel="Residual",title="Fit diagnostic")
fig.savefig(OUTPUT / "synthetic_ssb.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "synthetic_ssb.png")))
""")
md(r"""
### 17.2 Scan common phase at fixed depth and validate a held-out seed

Apply the common correction at the outgoing clock pulse of every full gate, using the sign from the measured clock mapping. Compare candidates with identical random words and initial states. Freeze training words covering all 12 initial states. The offline three-point phase sweep compiles only the first word. For tuning, aggregate all states and sequences before choosing a phase. Freeze parameters, then validate independent seeds and depths. [Tsai et al.](https://arxiv.org/html/2407.20184v2)
""")
code("""
ssb_phase_records = analysis.ssb_sequences(total_cz_counts=(4,),random_rf_count=4,repeats_per_state=1,seed=1171)
ssb_validation_records = analysis.ssb_sequences(total_cz_counts=(2,4,6),random_rf_count=4,repeats_per_state=1,seed=2171)
ssb_return_phases = SweepParameter("ssb_clock_return_phase", [float(clock_return_phase)+d for d in (-.1,0,.1)], "rad")

def cz_ssb_phase_scan(q, phase, record, gate_map, clock_inversion, uv_pulse, *, frequency, second_phase, prepare, readout, shots=2):
    exp = Experiment("cz_ssb_common_phase_scan",[q],metadata={"ssb_record":record,"complete_gate_count":record["total_cz_count"]})
    with exp.average(shots), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            for gate in record["word"]:
                if gate == "cz":
                    play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=phase)
                else:
                    pulse,rf_phase = gate_map[gate]
                    play(q.rf,pulse,phase=rf_phase*deg)
        readout()
    return exp

ssb_phase_preview = lab.compile(cz_ssb_phase_scan(q0,ssb_return_phases,ssb_phase_records[0],rf_gate_map,clock_pi,uv_gate_pulse,frequency=uv_drive_frequency,second_phase=uv_phase,**callbacks))
for record in [*ssb_phase_records,*ssb_validation_records]:
    assert record["rf_count"] == 13
    assert np.isclose(abs(analysis.global_word_unitary(record["word"])[0,0])**2,1)
(OUTPUT/"ssb_phase_training.json").write_text(json.dumps(ssb_phase_records,indent=2),encoding="utf-8")
(OUTPUT/"ssb_phase_validation.json").write_text(json.dumps(ssb_validation_records,indent=2),encoding="utf-8")
""")
md(r"""
### 17.3 Define a continuous amplitude/phase candidate

Retain the two-square-pulse baseline. Each new candidate applies clock π up → 2 µs → one continuous UV pulse → 2 µs → clock π down. The full gate preserves the frame through its UV waveform. These uncalibrated parameters illustrate a tunable family. See [benchmarking and waveform references](yb171_benchmarking_references.md).

Use a normalized `FlatTopGaussian` and local `PolynomialPhase`. With $u=t/T$, $\phi(u)=16\beta u^2(1-u)^2$ reaches β at the center and has zero phase and slope at both ends. Specify coefficients in ascending powers, in radians. The fixed zero linear coefficient avoids duplicating carrier detuning. Arbitrary Python waveform functions are unsupported.

Total Gaussian duration is `flat_duration + 2*cutoff_sigma*sigma` and its peak is normalized to 1. At 1.25 GS/s set `sample_interval=0.8*ns`. Duration also changes phase derivatives and instantaneous frequency. Inspect the waveform and frequency diagnostic. Measure Ω, Stark shifts, transfer function and scattering. Define four named β × plateau-duration candidates on the host, rebuilding coefficients rather than multiplying a `SweepParameter`. Preview one. The older `collective_cz_model` applies to two square pulses only.
""")

code("""
from taiyidsl import PolynomialPhase

shaped_candidates = []
uv_shaped_candidates = {}
for beta_optical in (-0.4, 0.4):
    for flat_us in (3.2, 4.0):
        candidate_id = f"smooth_beta{beta_optical:+.1f}_flat{flat_us:.1f}us"
        beta_drive = beta_optical / optical_mapping["rydberg"]["phase_scale"]
        phase_coefficients = [0, 0, 16 * beta_drive, -32 * beta_drive, 16 * beta_drive]
        candidate = {
            "candidate_id": candidate_id,
            "status": "uncalibrated; no atom data",
            "family": "flat_top_gaussian_and_zero_endpoint_polynomial_phase",
            "beta_optical_rad": beta_optical,
            "phase_coefficients_drive_rad": phase_coefficients,
            "phase_modulation_mode": "local",
            "flat_duration_us": flat_us,
            "sigma_us": 0.25,
            "cutoff_sigma": 4,
            "uv_total_duration_s": (flat_us + 2.0) * 1e-6,
            "sample_interval_s": 0.8e-9,
            "amplitude": q0.parameters.rydberg.pi_amplitude,
            "optical_detuning_hz": 0.0,
            "drive_frequency_hz": q0.parameters.rydberg.frequency,
            "clock_return_phase_rad": 0.0,
            "clock_gap_s": 2e-6,
            "pair": [q0.uid, q1.uid],
            "rf_spectator": spectator.uid,
            "required_initial_occupancy": "both pair sites loaded",
            "optical_mapping": dict(optical_mapping["rydberg"]),
        }
        shaped_candidates.append(candidate)
        uv_shaped_candidates[candidate_id] = FlatTopGaussian(
            flat_duration=flat_us * us,
            sigma=0.25 * us,
            cutoff_sigma=4,
            amplitude=candidate["amplitude"],
            sample_interval=0.8 * ns,
            phase_modulation=PolynomialPhase(tuple(phase_coefficients)),
        )

shaped_demo_candidate = shaped_candidates[0]  # Not an optimized selection.
uv_shaped_demo = uv_shaped_candidates[shaped_demo_candidate["candidate_id"]]
shaped_controls = {
    "frequency": shaped_demo_candidate["drive_frequency_hz"] * Hz,
    "amplitude": shaped_demo_candidate["amplitude"],
    "return_phase": shaped_demo_candidate["clock_return_phase_rad"] * rad,
}
display(shaped_candidates)
""")

md(r"""
### 17.4 Check closure, echo and Bell for the same candidate

`play_shaped_gate` lists the five steps of one complete gate. Each experiment shows its preparation, global RF, gate order and detection explicitly. Reset the three frames once at sequence entry. Preserve them within and between gates. Clock and UV cover q0/q1, RF also reaches the spectator.

For closure start in 11 and measure return, clock/Rydberg residuals and loss. A single-atom control needs actual single occupancy. Echo uses 2M complete gates and its χ interpretation assumes approximately diagonal action. Bell population and parity each use one gate. The four compiles preview closure stress, M=1 echo, four parity phases and one fixed SSB sequence. They cover one candidate.
""")

code("""
def play_shaped_gate(
    q, clock_inversion, uv_pulse, *, frequency, amplitude, return_phase
):
    play(q.clock, clock_inversion)
    delay(2 * us)
    play(q.rydberg, uv_pulse, frequency=frequency, amplitude=amplitude)
    delay(2 * us)
    play(q.clock, clock_inversion, phase=return_phase)


def shaped_pair_closure(
    q,
    amplitude_axis,
    frequency_axis,
    rf_inversion,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    return_phase,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_pair_closure",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "11 pair return",
            "complete_gate_count": 1,
            "readout_required": "joint logical/leak/loss",
        },
    )
    with exp.average(shots), exp.sweep(amplitude_axis), exp.sweep(frequency_axis):
        with exp.realtime():
            prepare()
            delay(10 * us)
            with exp.triggered_sequence(group="qubit_awg"):
                reset_phase(q.phase_frame("spin"))
                reset_phase(q.phase_frame("clock_control"))
                reset_phase(q.phase_frame("rydberg_control"))
                play(q.rf, rf_inversion)
                play_shaped_gate(
                    q,
                    clock_inversion,
                    uv_pulse,
                    frequency=frequency_axis,
                    amplitude=amplitude_axis,
                    return_phase=return_phase,
                )
            readout()
    return exp


def shaped_echo(
    q,
    rf_half,
    rf_inversion,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    return_phase,
    M=1,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_global_echo",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "global echo",
            "complete_gate_count": 2 * M,
            "M": M,
        },
    )
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf, rf_half, phase=90 * deg)
            for _ in range(M):
                play_shaped_gate(
                    q,
                    clock_inversion,
                    uv_pulse,
                    frequency=frequency,
                    amplitude=amplitude,
                    return_phase=return_phase,
                )
            play(q.rf, rf_inversion)
            for _ in range(M):
                play_shaped_gate(
                    q,
                    clock_inversion,
                    uv_pulse,
                    frequency=frequency,
                    amplitude=amplitude,
                    return_phase=return_phase,
                )
            play(q.rf, rf_inversion)
            play(q.rf, rf_half, phase=270 * deg)
        readout()
    return exp


def shaped_bell_population(
    q,
    return_phase_axis,
    rf_half,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_bell_population",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "Bell common-phase scan",
            "complete_gate_count": 1,
        },
    )
    with exp.average(shots), exp.sweep(return_phase_axis), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf, rf_half)
            play_shaped_gate(
                q,
                clock_inversion,
                uv_pulse,
                frequency=frequency,
                amplitude=amplitude,
                return_phase=return_phase_axis,
            )
            play(q.rf, rf_half, phase=270 * deg)
        readout()
    return exp


def shaped_bell_parity(
    q,
    phase_axis,
    rf_half,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    return_phase,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_bell_parity",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "Bell analysis phase",
            "complete_gate_count": 1,
            "target_bell_phase_rad": np.pi / 2,
        },
    )
    with exp.average(shots), exp.sweep(phase_axis), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf, rf_half)
            play_shaped_gate(
                q,
                clock_inversion,
                uv_pulse,
                frequency=frequency,
                amplitude=amplitude,
                return_phase=return_phase,
            )
            play(q.rf, rf_half, phase=270 * deg)
            play(q.rf, rf_half, phase=phase_axis)
        readout()
    return exp


shaped_stress_amplitude = SweepParameter(
    "shaped_stress_amplitude",
    [0.98 * shaped_controls["amplitude"], 1.02 * shaped_controls["amplitude"]],
    "dimensionless",
)
shaped_stress_detuning_hz = [-5000, 5000]
shaped_stress_frequency = SweepParameter(
    "shaped_stress_drive_hz",
    [
        shaped_demo_candidate["drive_frequency_hz"]
        + d / optical_mapping["rydberg"]["frequency_scale"]
        for d in shaped_stress_detuning_hz
    ],
    "Hz",
)
shaped_closure_preview = lab.compile(
    shaped_pair_closure(
        q0,
        shaped_stress_amplitude,
        shaped_stress_frequency,
        rf_pi,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        return_phase=shaped_controls["return_phase"],
        **callbacks,
    )
)
shaped_echo_preview = lab.compile(
    shaped_echo(
        q0,
        rf_pi2,
        rf_pi,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        M=1,
        **shaped_controls,
        **callbacks,
    )
)
shaped_parity_phase = SweepParameter("shaped_parity_phase", [0, 45, 90, 135], "deg")
shaped_parity_preview = lab.compile(
    shaped_bell_parity(
        q0,
        shaped_parity_phase,
        rf_pi2,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        **shaped_controls,
        **callbacks,
    )
)
# Build the population scan for inspection; do not compile an additional bank.
shaped_return_phase_axis = SweepParameter(
    "shaped_clock_return_phase", [-0.2, 0, 0.2], "rad"
)
shaped_population_exp = shaped_bell_population(
    q0,
    shaped_return_phase_axis,
    rf_pi2,
    clock_pi,
    uv_shaped_demo,
    candidate=shaped_demo_candidate,
    frequency=shaped_controls["frequency"],
    amplitude=shaped_controls["amplitude"],
    **callbacks,
)
""")

md(r"""
### 17.5 Compare frozen training circuits with held-out circuits

Keep the pair, balanced set of 12 initial states, depth, repetitions per state and 33 common RF pulses fixed. Replace every `cz` token, including initialization and recovery, with the candidate's complete gate. The ideal-CZ recovery defines the target.

Train with seed 171 and hold out seed 3171 at the same depth, state count and RF count. Freeze candidate parameters and analysis before inspecting validation. Both manifests store complete gate words. The candidate manifest records pair, detection requirements, stress and parameters. Actual execution must also record reloads, SPAM drift and shot randomization.
""")

code("""
def shaped_ssb_word(
    q,
    record,
    gate_map,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    return_phase,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_ssb_fixed_word",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "fixed-word symmetric-state return",
            "ssb": dict(record),
            "complete_gate_count": record["total_cz_count"],
            "expected_readout": "00 only for the ideal target and calibrated SPAM",
        },
    )
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            for gate in record["word"]:
                if gate == "cz":
                    play_shaped_gate(
                        q,
                        clock_inversion,
                        uv_pulse,
                        frequency=frequency,
                        amplitude=amplitude,
                        return_phase=return_phase,
                    )
                else:
                    pulse, phase = gate_map[gate]
                    play(q.rf, pulse, phase=phase * deg)
        readout()
    return exp


shaped_ssb_train = [dict(record) for record in ssb_records]
shaped_ssb_depths = sorted({record["total_cz_count"] for record in shaped_ssb_train})
shaped_ssb_heldout = analysis.ssb_sequences(
    total_cz_counts=tuple(shaped_ssb_depths),
    random_rf_count=24,
    repeats_per_state=2,
    seed=3171,
)
for records in (shaped_ssb_train, shaped_ssb_heldout):
    assert all(record["rf_count"] == 33 for record in records)
    assert all(
        record["word"].count("cz") == record["total_cz_count"] for record in records
    )
    for depth in shaped_ssb_depths:
        states = [
            record["initial_state"]
            for record in records
            if record["total_cz_count"] == depth
        ]
        assert len(states) == 24 and all(
            states.count(state) == 2 for state in range(12)
        )
assert {record["seed"] for record in shaped_ssb_train} == {171}
assert {record["seed"] for record in shaped_ssb_heldout} == {3171}
assert {record["word"] for record in shaped_ssb_train}.isdisjoint(
    {record["word"] for record in shaped_ssb_heldout}
)

shaped_ssb_preview = lab.compile(
    shaped_ssb_word(
        q0,
        ssb_record,
        rf_gate_map,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        **shaped_controls,
        **callbacks,
    )
)
shaped_candidate_manifest = {
    "status": "offline candidate definitions; no measured score",
    "candidates": shaped_candidates,
    "preview_candidate_id": shaped_demo_candidate["candidate_id"],
    "ssb_train_file": "shaped_ssb_train.json",
    "ssb_heldout_file": "shaped_ssb_heldout.json",
    "ssb_train_seed": 171,
    "ssb_heldout_seed": 3171,
    "ssb_total_cz_counts": shaped_ssb_depths,
    "ssb_rf_count": 33,
    "ssb_states": 12,
    "ssb_repeats_per_state": 2,
    "stress_amplitude_fraction": [0.98, 1.02],
    "stress_optical_detuning_hz": shaped_stress_detuning_hz,
    "readout_categories": ["00", "01", "10", "11", "leak", "loss", "invalid"],
    "hardware_validation": "not performed",
    "acceptance_policy": {
        "status": "freeze actual shot counts, confidence and thresholds before data",
        "constraints": [
            "invalid fraction",
            "loss / valid",
            "leak / valid",
            "logical / valid",
        ],
        "training_target": (
            "worst-group SSB return lower confidence bound; "
            "target Bell phase cross-check"
        ),
        "validation": (
            "held-out seed and all stress points; "
            "never an average gate fidelity claim"
        ),
    },
}
for name, payload in (
    ("shaped_candidate_manifest.json", shaped_candidate_manifest),
    ("shaped_ssb_train.json", shaped_ssb_train),
    ("shaped_ssb_heldout.json", shaped_ssb_heldout),
):
    (OUTPUT / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(
    "Shaped SSB definitions: train",
    len(shaped_ssb_train),
    "held-out",
    len(shaped_ssb_heldout),
    "; only one word compiled.",
)
""")

md(r"""
### 17.6 Verify the DAC waveform and select candidates under constraints

Read the compiled UV channel from the final DAC bank and independently recompute envelope and phase to 1 LSB. Check complete-gate counts. Samples use $u=n/N$, so the analytic endpoint $u=1$ is outside the last active sample. The stress grid displays planned amplitude/frequency settings. Its colors contain no observations.

Predeclare limits on invalid fraction, logical survival, leakage and loss. Use valid trials including leak/loss as the denominator and report invalid shots separately, plus loading rate and any loading filters. Keep same-shot site classifications and frame state together. Different detectors or sequences on different shots cannot supply joint observations.

Among candidates satisfying predeclared limits at every stress point, compare worst lower confidence bounds on training SSB return across depths and inputs, then check consistency with target-phase Bell. Freeze shot counts, confidence level and thresholds in advance. Return alone admits identity. Echo can hide local phase. SSB, Bell-output fidelity and the square-pulse model each quantify different properties.

Screen with closure and M=1 echo, refine with M=3, Bell phase and training SSB, and validate finalists with held-out seeds, repeated dates/pairs and amplitude/frequency stress. Match RF-only and clock-only control waits to the new complete shaped gate duration.
""")

code("""
# Advanced inspection only: the experiment functions above use public DSL APIs.
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window

assert [point.axis_indices for point in shaped_closure_preview.points] == [
    (0, 0),
    (0, 1),
    (1, 0),
    (1, 1),
]
shaped_sample_receipts = []
for point in shaped_closure_preview.points:
    uv_event = next(
        event
        for event in point.logical_events
        if event.kind == "play" and event.signal == q0.rydberg
    )
    assert np.isclose(
        uv_event.params["amplitude"],
        float(shaped_stress_amplitude.values[point.axis_indices[0]]),
    )
    assert np.isclose(
        uv_event.params["frequency"],
        float(shaped_stress_frequency.values[point.axis_indices[1]]),
    )
    group = next(
        event.params
        for event in point.physical_events
        if event.kind == "spectrum_program"
    )
    program = group["cards"]["B"]["program"]
    uv_record = next(pulse for pulse in program["pulses"] if pulse["channel"] == 1)
    assert uv_record["phase_steps"] == []
    assert uv_record["phase_modulation"]["kind"] == "polynomial"
    np.testing.assert_allclose(
        uv_record["phase_modulation"]["coefficients"],
        shaped_demo_candidate["phase_coefficients_drive_rad"],
        atol=1e-14,
    )
    assert uv_record["duration_samples"] == round(
        shaped_demo_candidate["uv_total_duration_s"] * 1.25e9
    )
    assert abs(uv_record["phase_evolution"]["modulation_end_rad"]) < 1e-12
    assert uv_record["phase_evolution"]["terminal_modulation_phase_rad"] == 0
    n = np.arange(uv_record["duration_samples"])
    u = n / uv_record["duration_samples"]
    shape = uv_record["envelope"]
    edge = shape["sigma_samples"] * shape["cutoff_sigma"]
    distance = np.maximum(edge - np.minimum(n, len(n) - n), 0)
    base = np.exp(-0.5 * shape["cutoff_sigma"] ** 2)
    envelope = (np.exp(-0.5 * (distance / shape["sigma_samples"]) ** 2) - base) / (
        1 - base
    )
    phase_local = sum(
        c * u**i
        for i, c in enumerate(shaped_demo_candidate["phase_coefficients_drive_rad"])
    )
    phase = (
        uv_record["phase_rad"]
        + 2 * np.pi * uv_record["frequency_hz"] * n / 1.25e9
        + uv_record["phase_scale"] * phase_local
    )
    expected_dac = np.rint(32767 * uv_record["amplitude"] * envelope * np.cos(phase))
    start = uv_record["start_sample"]
    uv_column = program["card_spec"]["active_channels"].index(1)
    final_dac = reconstruct_window(program, start, start + len(n) + 1)[:, uv_column]
    np.testing.assert_allclose(final_dac[:-1], expected_dac, atol=1)
    assert final_dac[0] == 0 and final_dac[-1] == 0
    shaped_sample_receipts.append(
        {
            "indices": list(point.axis_indices),
            "sample_count": len(n),
            "max_error_lsb": float(np.max(abs(final_dac[:-1] - expected_dac))),
            "physical_frequency_hz": uv_record["frequency_hz"],
            "physical_peak_amplitude": uv_record["amplitude"],
        }
    )

for preview, complete_gates in (
    (shaped_closure_preview, 1),
    (shaped_echo_preview, 2),
    (shaped_parity_preview, 1),
    (shaped_ssb_preview, ssb_record["total_cz_count"]),
):
    for point in preview.points:
        events = [event for event in point.logical_events if event.kind == "play"]
        assert sum(event.signal == q0.rydberg for event in events) == complete_gates
        assert sum(event.signal == q0.clock for event in events) == 2 * complete_gates
        resets = [
            event for event in point.logical_events if event.kind == "reset_phase"
        ]
        assert len(resets) == 3
        assert all(event.start_mu == events[0].start_mu for event in resets)
    assert not any(
        item["kind"] == "spectrum_modulation_frequency_range"
        for item in preview.diagnostics
    )

fig, axes = plt.subplots(1, 3, figsize=(12, 3))
axes[0].plot(n / 1.25e9 * 1e6, envelope)
axes[0].set(
    xlabel="UV time (us)", ylabel="Peak-normalized envelope", title="Candidate shape"
)
axes[1].plot(n / 1.25e9 * 1e6, phase_local)
axes[1].set(
    xlabel="UV time (us)",
    ylabel="Drive phase modulation (rad)",
    title="One continuous pulse",
)
for i, amplitude_fraction in enumerate((0.98, 1.02)):
    for j, detuning_hz in enumerate(shaped_stress_detuning_hz):
        axes[2].plot(detuning_hz / 1000, amplitude_fraction, "o", color="tab:blue")
        axes[2].annotate(
            f"point {2 * i + j}",
            (detuning_hz / 1000, amplitude_fraction),
            xytext=(4, 4),
            textcoords="offset points",
        )
axes[2].set(
    xlabel="Optical detuning offset (kHz)",
    ylabel="Amplitude / candidate",
    title="Planned stress points; no atom data",
    xlim=(-7, 9),
    ylim=(0.97, 1.035),
)
fig.tight_layout()
fig.savefig(OUTPUT / "shaped_candidate_and_stress_grid.png", dpi=150)
plt.close(fig)
display(Image(filename=str(OUTPUT / "shaped_candidate_and_stress_grid.png")))
(OUTPUT / "shaped_waveform_software_checks.json").write_text(
    json.dumps(
        {
            "scope": "final DAC vs analytic envelope and phase; no hardware",
            "candidate_id": shaped_demo_candidate["candidate_id"],
            "points": shaped_sample_receipts,
        },
        indent=2,
    ),
    encoding="utf-8",
)
""")

md(r"""
## 18. Shot records, uncertainty and calibration updates

Record run ID, timestamp, experiment, pair/site, occupancy, field/trap/temperature, configuration revision/hash, scan values/units, sequence seed/word/hash, attempt/shot, image references, 0/1/c/r/loss/invalid classification, classifier version and selection rules. Preserve joint results from the same shot.

Interleave references, depth and phase, and insert RF/clock monitors regularly. Use Wilson/Beta intervals or likelihood for binomial probabilities. For measured fringes, Rabi and decay use counts and an appropriate heteroscedastic or hierarchical model. The synthetic least-squares example supplies no measured confidence intervals.

| Update | Evidence | Recheck after rebuilding |
|---|---|---|
| RF frequency | Spectroscopy and Ramsey quadratures | Full site distribution |
| RF π/π2 amplitudes/durations | Rabi, ALLXY and amplification | Named pulses, experiment and compilation |
| Clock amplitude/duration/frequency | Transfer, return and optical Ramsey | Clock phase and return |
| CZ duration/detuning/phase | Closure, echo and loss | Bell population/parity and SSB |
| Common phase | Bell phase and training SSB | Independent seed |
| Zone members, wiring or triggers | Configuration and transport records | Descriptor/setup/lab and output coverage |

Qubit edits affect newly constructed pulses and experiments. The next cell writes a proposal for review. For a measured update such as `q0.parameters.rf.pi_duration = measured_pi_s`, rebuild the corresponding pulse and experiment. Shared global RF remains one physical control.
""")
code("""
calibration_proposal = {
    "status":"example_only_not_applied", "source":"synthetic/model",
    "rf_pi_duration_s":float(np.pi/fit_rabi[2]),
    "cz_seed_dimensionless":cz_seed,
    "cz_uv_duration_quantized_s":uv_duration_s,
    "cz_complete_gate_time_s":GATE_TIME_S,
    "required_before_application":["raw counts and fit residuals","site uniformity","SPAM/loss/leakage","independent validation seed"],
}
(OUTPUT/"calibration_proposal.example.json").write_text(json.dumps(calibration_proposal,indent=2),encoding="utf-8")
shot_schema_example = {
    "run_id":"EXAMPLE_NO_MEASUREMENTS", "experiment":"ssb", "pair":["q0","q1"],
    "zone_members":["q0","q1"], "rf_affected":["q0","q1","spectator"],
    "total_cz":2, "rf_half_pi_count":33, "initial_state":ssb_record["initial_state"],
    "seed":171,"sequence":0,"shot":0,"attempt":0,
    "outcomes":None,"image_reference":None,"detector_status":"not_connected",
    "postselection_rule":"predeclared; report raw and selected separately",
}
(OUTPUT/"shot_schema.example.json").write_text(json.dumps(shot_schema_example,indent=2),encoding="utf-8")
""")
md(r"""
### Implemented extensions and laboratory integrations

The preceding sections provide compilable scans and pulse sequences. See [method references](yb171_benchmarking_references.md). The following measurements need the indicated instrumentation.

| Experiment | Measurement or interface | Observable |
|---|---|---|
| Field and RF drift | Interleaved Ramsey quadratures and calibrated frequency slope | Site resonance versus time |
| Transport coherence | Compare configured DDS `move` to equal-duration stationary Ramsey, including settling and return | Phase, contrast, survival |
| Heating/cooling | Sideband spectroscopy or release/recapture before and after transport | Temperature, motional occupancy, loss |
| Spin lock | Section 6.3 amplitude × time and independent response/noise calibration | T1ρ and spectral noise |
| Clock T1 | Section 11.1 matched holds with c-selective and repump controls | c survival versus loss |
| Rydberg lifetime | Section 12.1 plus trap control, recapture and decay-branch detection | Lifetime and detection efficiency |
| Zone crosstalk | Storage Ramsey with clock only, UV only and complete gates of matched duration | Spectator phase, contrast, loss |
| Pair crosstalk | Single versus simultaneous pairs over separation | Pair-resolved Bell/SSB and correlations |
| AOM response | Photodetection or heterodyne measurement of edges, phase and frequency–amplitude coupling | Optical power and phase timeline |

For a daily check measure survival/SPAM → RF Ramsey and area → clock return/phase → Rydberg closure → CZ echo → Bell phase → short SSB. Define drift thresholds from the laboratory error budget and rerun full tune-up when exceeded.
""")
md(r"""
## 19. Offline checks and outputs

Compile representative points in each experiment family. RF RB and SSB words are frozen and verified for ideal recovery. All 21 ALLXY pairs are built and representative pairs are compiled. Pulse and trigger tables come from compilation.

Inspect `output/yb171_tuneup/` for reports, model/synthetic plots, RF RB/SSB/echo manifests, proposed calibrations, shot schema and offline check summary. After Run All, open the [RF Ramsey report](../../output/yb171_tuneup/rf_ramsey_report/index.html) and [CZ echo report](../../output/yb171_tuneup/cz_echo_report/index.html). The pulse sheet and waveform use compiled events and final samples. Convert IR `start_mu` using `compiled.setup.tick_seconds` because hybrid ticks can differ from the ARTIQ reference period. Record kernel execution, ARTIQ compilation, browser rendering and optical timing checks separately.
""")
code("""
# Check critical physical scope/counts on actual compiled IR.
rf_events = [e for e in ramsey_preview.points[0].logical_events if e.kind=="play"]
assert len(rf_events)==2
assert all(e.params["global_operation"]["targets"]==["q0","q1","spectator"] for e in rf_events)
assert all(e.params["frequency"]==31000 for e in rf_events)
echo_events = [e for e in echo_preview.points[1].logical_events if e.kind=="play"]
assert len(echo_events)==3
echo_gaps_s = [(echo_events[i+1].start_mu-echo_events[i].end_mu)*echo_preview.setup.tick_seconds for i in (0,1)]
np.testing.assert_allclose(echo_gaps_s,[100e-6,100e-6],rtol=0,atol=1e-12)
ssb_events = [e for e in ssb_preview.points[0].logical_events if e.kind=="play"]
assert sum(e.signal==q0.rf for e in ssb_events)==33
assert sum(e.signal==q0.clock for e in ssb_events)==2*ssb_record["total_cz_count"]
assert sum(e.signal==q0.rydberg for e in ssb_events)==2*ssb_record["total_cz_count"]
assert all(e.params["global_operation"]["targets"]==["q0","q1"] for e in ssb_events if e.signal in (q0.clock,q0.rydberg))
# New 2D authoring contracts: final frequencies, Cartesian order and edge gaps.
assert [p.axis_indices for p in rf_chevron_preview.points] == [(i,j) for i in range(3) for j in range(3)]
assert len(list(rf_chevron_preview.execution_order())) == 18
for point in rf_chevron_preview.points:
    event = next(e for e in point.logical_events if e.kind=="play")
    assert event.params["frequency"] == point.coordinates["rf_chevron_hz"]
    assert np.isclose(event.duration_mu*rf_chevron_preview.setup.tick_seconds,point.coordinates["rf_chevron_time"])
    assert event.params["global_operation"]["targets"] == ["q0","q1","spectator"]
for point in rf_ramsey_map_preview.points:
    events = [e for e in point.logical_events if e.kind=="play"]
    assert len(events)==2 and sum(e.kind=="reset_phase" for e in point.logical_events)==1
    assert all(e.params["frequency"]==point.coordinates["rf_ramsey_hz"] for e in events)
    assert all(e.params["phase"]==0 for e in events)
    assert np.isclose((events[1].start_mu-events[0].end_mu)*rf_ramsey_map_preview.setup.tick_seconds,point.coordinates["rf_dark_time"])
# Same +1kHz / 10us point as the earlier 1D Ramsey: actual RF carrier phases agree.
old_group = next(e.params for e in ramsey_preview.points[0].physical_events if e.kind=="spectrum_program")
map_group = next(e.params for e in rf_ramsey_map_preview.points[6].physical_events if e.kind=="spectrum_program")
for old_pulse,new_pulse in zip(old_group["cards"]["A"]["program"]["pulses"],map_group["cards"]["A"]["program"]["pulses"],strict=True):
    for field in ("start_sample","duration_samples","frequency_hz","amplitude","phase_rad"):
        assert old_pulse[field] == new_pulse[field]
for light,idle in zip(shelving_map_preview.points,shelving_idle_preview.points,strict=True):
    light_rf = [e for e in light.logical_events if e.kind=="play" and e.signal==q0.rf]
    idle_rf = [e for e in idle.logical_events if e.kind=="play" and e.signal==q0.rf]
    assert [e.start_mu for e in light_rf] == [e.start_mu for e in idle_rf]
    assert light.duration_mu == idle.duration_mu
for preview in (clock_chevron_preview,rydberg_chevron_preview,cz_closure_preview,cz_echo_map_preview):
    for point in preview.points:
        for event in point.logical_events:
            if event.kind=="play" and event.signal in (q0.clock,q0.rydberg):
                assert event.params["global_operation"]["targets"] == ["q0","q1"]
for point in xy8_x_preview.points:
    events = [e for e in point.logical_events if e.kind=="play"]
    np.testing.assert_allclose([e.params["phase"] for e in events[1:-1]],np.deg2rad([0,90,0,90,90,0,90,0]))
    total_dark = sum(events[i+1].start_mu-events[i].end_mu for i in range(len(events)-1))*xy8_x_preview.setup.tick_seconds
    assert np.isclose(total_dark,16*point.coordinates["xy8_half_gap"])
for point in area_pi_preview.points:
    assert sum(e.kind=="play" for e in point.logical_events)==1+2*point.coordinates["area_blocks"]
for point in cz_repetition_preview.points:
    count = point.coordinates["complete_cz_repeats"]
    assert sum(e.kind=="play" and e.signal==q0.clock for e in point.logical_events)==2*count
    assert sum(e.kind=="play" and e.signal==q0.rydberg for e in point.logical_events)==2*count
# Summarize existing compiled results.
previews = [
    rf_spec_preview, rf_rabi_preview, ramsey_preview, echo_preview, cpmg_preview,
    axis_preview, allxy_preview, amplification_preview, rb_preview,
    clock_spec_preview, clock_rabi_preview, clock_optical_preview,
    clock_phase_preview, idle_phase_preview, return_train_preview, clock_rb_preview,
    rydberg_spec_preview, rydberg_rabi_preview, cz_echo_preview,
    population_preview, parity_preview, *echo_benchmark_previews, ssb_preview,
    rf_chevron_preview, rf_amplitude_preview, rf_ramsey_map_preview, rf_ramsey_phase_preview,
    xy8_x_preview, xy8_y_preview, spin_lock_preview, population_zero_preview, population_one_preview,
    area_pi_preview, area_half_preview, clock_chevron_preview, clock_ramsey_map_preview,
    shelving_map_preview, shelving_idle_preview, stark_preview,
    clock_hold_return_preview, clock_hold_residual_preview, clock_hold_idle_preview,
    rydberg_chevron_preview, rydberg_return_preview, cz_closure_preview, cz_echo_map_preview,
    cz_repetition_preview, cz_loss_zero_preview, cz_loss_plus_preview, cz_loss_one_preview,
    ssb_phase_preview, shaped_closure_preview, shaped_echo_preview, shaped_parity_preview, shaped_ssb_preview,
]
if rb_full_compiled is not None:
    previews.append(rb_full_compiled)
compile_log = [
    {"experiment": result.name, "points": len(result.points),
     "shots": result.shots, "validation": "offline DSL/Spectrum only"}
    for result in previews
]
display(compile_log)
verification = {"scope":"offline previews, ideal algebra, synthetic analysis",
                "compile_log":compile_log,"rf_rb_frozen":len(rb_sequences),"ssb_frozen":len(ssb_records),
                "rf_rb_basis":RB_BASIS_NAME,"rf_rb_preview_conditions":len(rb_preview.points),
                "rf_rb_full_compiled":rb_full_compiled is not None,
                "ssb_rf_half_pi_count":33,"hardware_executed":False,
                "real_artiq_compilation":False,"browser_render_verified":False,
                "measured_quantum_fidelity":None}
(OUTPUT/"offline_verification.json").write_text(json.dumps(verification,indent=2),encoding="utf-8")
print("Offline checks passed. No laboratory measurements were made.")
""")
md(r"""
## 20. Connect explicitly to configured laboratory hardware

Run All stops at offline compilation and visualization. Supply laboratory device identities, clocks, calibrated qubits and actual preparation/readout. Rebuild named pulses with measured values. The following Markdown example is outside Run All.

```python
real_rf_pi2 = Square(
    duration=calibrated_q0.parameters.rf.pi2_duration,
    amplitude=calibrated_q0.parameters.rf.pi2_amplitude,
)
real_lab = LabConfig(
    setup=real_setup,
    qubits=calibrated_qubits,
    emulation=False,
    artiq_python=artiq_python,
)
real_exp = rf_ramsey(
    calibrated_q0, tau, real_rf_pi2,
    prepare=real_prepare, readout=real_readout, shots=100,
)
real_compiled = real_lab.compile(real_exp)
with real_lab.connect() as session:
    receipt = session.run(real_compiled)
```

`real_setup` contains verified wiring/calibrations. Register every atom exposed to shared outputs in `calibrated_qubits`. Set `artiq_python` to the laboratory ARTIQ environment. `connect` verifies identities and clocks and `run` uploads and executes. No clock-session token is needed. The execution receipt records instrument commands. Camera triggering, frame retrieval and durable commit are distinct steps. `HardwareSession.run()` here has no detector-adapter injection. See [readout adapters](../readout_adapters.md), [durable execution](../streamed_execution.md) and [native components](../native_components.md).
""")
md(r"""
## 21. References and applicability

1. The supplied design note informed the ordering of echo, Bell and SSB experiments. The notebook independently checks the ideal relations using matrices. [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)
2. RF nuclear-spin gates, collective entangling pulses and Bell characterization in ¹⁷¹Yb. The Rydberg excitation path differs from the clock shelving used here. [Ma et al., PRX 12, 021028 (2022)](https://doi.org/10.1103/PhysRevX.12.021028)
3. Zone-wide clock/Rydberg control, full gate definition and component benchmarks. Local Raman control in the experiment enabled protocols unavailable with global RF alone. [Muniz et al., arXiv:2411.11708](https://arxiv.org/abs/2411.11708)
4. Ground-state nuclear-spin RF, global rotations and QND readout in ¹⁷¹Yb. [Huie et al., PRX Quantum 4, 030337 (2023)](https://doi.org/10.1103/PRXQuantum.4.030337)
5. SSB, symmetric-subspace metrics and fidelity response to noise. Reassess model assumptions for the complete clock round trip used here. [Tsai et al., PRX Quantum 6, 010331 (2025)](https://doi.org/10.1103/PRXQuantum.6.010331)

See the [notebook generator](../../scripts/build_yb171_tuneup_notebook.py), [numerical helpers](../../examples/yb171_tuneup_analysis.py) and [targeted mathematical tests](../../tests/test_yb171_tuneup_analysis.py). Experiment functions explicitly implement the physical pulse sequences.
""")

learning_notes = {
    "## 3.": r"""
Treat readout as a measurement channel with misclassification and erasure, $p_{\rm obs}=M p_{\rm true}$. Inversion requires identifiable states covering the actual leakage/loss branches. Use same-batch empty traps and known states and check raw denominators before correction.

The classification scheme must match the laboratory detector. [Huie et al.](https://doi.org/10.1103/PRXQuantum.4.030337) [Muniz et al.](https://arxiv.org/html/2411.11708v2#A6)
""",
    "## 4.": r"""
In the rotating-wave approximation, $\Omega_{\rm eff}=\sqrt{\Omega^2+\Delta^2}$ and $P_1=(\Omega/\Omega_{\rm eff})^2\sin^2(\Omega_{\rm eff}t/2)$. At fixed time the detuning sweep gives the spectrum. A long weak probe improves resolution but increases susceptibility to field drift. Ω and Δ in this expression use rad/s, while the scan uses Hz.

The points, powers and fit settings here are teaching examples. [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028) [Huie et al.](https://arxiv.org/abs/2305.02926)
""",
    "## 5.": r"""
On resonance $P_1=\sin^2(\Omega t/2)$ and $t_\pi=\pi/\Omega$. The example fits $a-b e^{-t/T_R}\cos(\Omega t)$. Detuning reduces amplitude and increases oscillation frequency. Calibrate resonance before area and check pulse edges using repetitions. The Rabi envelope time and Ramsey $T_2^*$ have different meanings.

Check residuals before choosing a phenomenological damping envelope. [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028)
""",
    "## 6.": r"""
The first π/2 rotates population to transverse coherence, and the final π/2 maps accumulated phase back to population. A common model is $P_1=a+b\exp[-(\tau/T_2^*)^\alpha]\cos(2\pi\delta f\tau+\phi)$. A single quadrature can leave the detuning sign ambiguous.

The echo π rotation reverses static detuning in the second half, suppressing quasi-static broadening while retaining faster noise and pulse errors. Fit against total dark time 2τ. Ramsey and echo need not share an envelope exponent.

For CPMG, the initial X90 creates −Y magnetization. Subsequent Y180 pulses refocus about that axis, limiting accumulation of area errors. The final −X90 maps back for readout. Different pulse trains have different noise filter functions.

The host frequency-lock outline is an extension in this workbook. [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028) [Hahn](https://doi.org/10.1103/PhysRev.80.580) [Meiboom and Gill](https://122.physics.ucdavis.edu/sites/default/files/files/Pulse%20NMR/Meiboom_Gill_RevSciInstr.29.6881_1958.pdf)
""",
    "## 7.": r"""
X and Y rotations do not commute. A fixed area, detuning or axis error gives different residuals for different pulse pairs. ALLXY orders those residuals into a staircase, while repeated gates amplify coherent errors. Cross-check against spectroscopy and Ramsey.

The original microwave-qubit example supplies the rotation algebra. Its hardware noise model requires separate validation for global RF. [Reed, Yale thesis](https://rsl.yale.edu/sites/default/files/2024-08/2013-RSL-Thesis-Matthew-Reed.pdf)
""",
    "## 8.": r"""
Clifford randomization averages many orientation-dependent errors into a decay. Ideal recovery returns to the input state. SPAM usually contributes to A/B and per-Clifford errors to p. Gate dependence, correlations or leakage can invalidate a single exponential.

All sites receive the same word and retain separate statistics. [Magesan et al.](https://doi.org/10.1103/PhysRevLett.106.180504) [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028)
""",
    "## 9.": r"""
The narrow clock transition maps laser detuning to transfer. Differential trap shifts, motion/sidebands and Zeeman structure can all affect the spectrum. Switching the initial spin tests selectivity.

Clock duration Rabi follows two-level rotation algebra with an optical reference. Trapping, lifetime and recovery of c affect the observed signal. Best single-pass transfer may differ from the best coherent round trip.

The example IF and square pulses provide a starting point. [Muniz et al.](https://arxiv.org/html/2411.11708v2)
""",
    "## 10.": r"""
In this convention two ideal clock π pulses give the amplitude $-e^{i(\phi_{\rm in}-\phi_{\rm out})}$ on $|1\rangle$. Nuclear RF Ramsey reads the phase relative to uncoupled 0. Optical Ramsey reads coherence between 1 and c. Scan a full period and fit both sine and cosine quadratures.

The sign follows the notebook's rotation convention. [Muniz et al.](https://arxiv.org/html/2411.11708v2) [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)
""",
    "## 11.": r"""
Population return is insensitive to deterministic phase, RF Ramsey makes phase and decoherence visible, and optical RB averages control error on the optical Bloch sphere. RF preparation errors and SPAM cannot simply be divided out of a repeated-gate result.

Optical 1Q RB is an optional extension here. [Muniz et al.](https://arxiv.org/html/2411.11708v2) [Magesan et al.](https://doi.org/10.1103/PhysRevLett.106.180504)
""",
    "## 12.": r"""
Interactions shift rr by V. Under strong blockade, cc couples to the symmetric state $(cr+rc)/\sqrt2$ at $\sqrt2\Omega$. Compare single-atom and pair spectra, oscillations and double excitation to distinguish power drift from blockade. Failed recapture also contributes to loss.

The excitation paths differ across platforms. Use the collective dynamics and calibration logic with the laboratory-specific coupling. [Muniz et al.](https://arxiv.org/html/2411.11708v2) [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028)
""",
    "## 13.": r"""
The two-detuned-pulse model initializes a complete shelving/UV/return experiment. Recalibrate the combined gate. [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028) [Muniz et al.](https://arxiv.org/html/2411.11708v2)
""",
    "### 13.1": r"""
The finite-blockade curves are generated by the workbook Hamiltonian. [Muniz et al.](https://arxiv.org/html/2411.11708v2)
""",
    "## 14.": r"""
The matrix checks below establish the diagonal-model identity. Test the diagonal assumption separately in hardware. [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a) [Muniz et al.](https://arxiv.org/html/2411.11708v2)
""",
    "## 15.": r"""
The Bell target phase π/2 follows the rotation conventions above. [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a) [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028)
""",
    "### Optional extension": r"""
The identification formula is a workbook derivation for the diagonal model. [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)
""",
    "## 16.": r"""
The random rotations, recovery and fit here implement the simplified diagnostic. [Muniz et al.](https://arxiv.org/html/2411.11708v2) [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)
""",
    "## 17.": r"""
Fixing RF count limits RF overhead varying with CZ depth. Correlated noise can still bias results. [Tsai et al.](https://arxiv.org/html/2407.20184v2#A5) [Design note](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)
""",
    "### 17.1": r"""
The paper uses its own likelihood and readout corrections. This workbook illustrates a synthetic least-squares fit. Strontium leakage corrections require separate validation in Yb. [Tsai et al.](https://arxiv.org/html/2407.20184v2)
""",
    "### Implemented extensions": r"""
Transport integration, AOM detection and the daily check are laboratory engineering suggestions. [Ma et al.](https://doi.org/10.1103/PhysRevX.12.021028) [Huie et al.](https://doi.org/10.1103/PRXQuantum.4.030337) [Muniz et al.](https://arxiv.org/html/2411.11708v2)
""",
}
for cell in cells:
    if cell.cell_type == "markdown":
        for prefix, note in learning_notes.items():
            if cell.source.startswith(prefix):
                cell.source += "\n\n" + textwrap.dedent(note).strip()
                break

for index, cell in enumerate(cells):
    cell.id = f"yb171-tuneup-{index:03d}"
notebook = nbformat.v4.new_notebook(cells=cells)
notebook.metadata.kernelspec = {
    "display_name": "Python 3 (project)",
    "language": "python",
    "name": "python3",
}
notebook.metadata.language_info = {"name": "python", "version": "3.12"}
nbformat.validate(notebook)
destination = ROOT / "docs/tutorials/yb171_global_rf_tuneup.ipynb"
nbformat.write(notebook, destination)
subprocess.run(
    [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        "I,F401",
        "--fix",
        str(destination),
    ],
    check=True,
)
subprocess.run([sys.executable, "-m", "ruff", "format", str(destination)], check=True)
print(destination)
