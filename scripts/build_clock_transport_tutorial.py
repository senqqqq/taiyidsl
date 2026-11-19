"""Derive a separate transport lesson from the saved plain Ramsey notebook."""

# Tutorial prose is intentionally kept as editable paragraphs.
# ruff: noqa: E501
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/tutorials/ramsey_clock_plain.ipynb"
DESTINATION = ROOT / "docs/tutorials/ramsey_clock_transport.ipynb"


def build():
    base = nbformat.read(BASE, as_version=4)
    cells = []

    def md(source):
        cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))

    def code(source):
        source = textwrap.dedent(source).strip() + "\n"
        formatted = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--stdin-filename",
                "lesson.py",
                "-",
            ],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        cells.append(nbformat.v4.new_code_cell(formatted.strip()))

    def original(prefix):
        found = [
            c.source
            for c in base.cells
            if c.cell_type == "code" and c.source.startswith(prefix)
        ]
        if len(found) != 1:
            raise ValueError(f"Expected one base cell starting with {prefix!r}")
        return found[0]

    md(r"""
    # Clock Ramsey with a round trip to the interaction zone

    Extend [plain clock Ramsey](ramsey_clock_plain.ipynb) with an outbound `move(q0, to=iz0[0], ...)` before the clock gate and a return to `q0.position` afterward. Keep the same single qubit, wiring, native preparation and readout, named pulses and scan of the second RF phase.

    | Card | Mode and role | Output in this experiment |
    |---|---|---|
    | A | AWG, global RF | Two 100 µs pulses at 31 kHz and amplitude 0.2 |
    | B | AWG, clock and Rydberg in the interaction zone | Clock at 200 kHz for 200 µs at 0.2. Rydberg stays at zero. |
    | C | DDS20, X/Y AOD | Move from (0,0) to (30,10) µm and back. Both tones maintain finite amplitude. |

    The lesson follows the device databases, descriptor, lab and qubit settings, native declarations, experiment, compilation, visualization and hardware connection. Run All compiles and exports offline. The GreenMOT and camera TTLs model timing. Positions are planned positions and card C curves predict commands. Measured images, analog output and actual atom preparation require laboratory equipment.

    Run `uv sync --locked --group notebooks` and select the project CPython 3.12 kernel. Results go to `output/ramsey_clock_transport/`. After changing an input, rerun the affected construction and subsequent cells.
    """)
    code("""
    import json
    from pathlib import Path

    import numpy as np
    from IPython.display import Markdown, display

    from taiyidsl import (
        IZ, DeviceSetup, Experiment, Hz, LabConfig, MoveProfile, NativeKernelPackage,
        Position, Qubit, Square, SweepParameter, TransportConfig,
        delay, kHz, move, ms, ns, play, reset_phase, um, us,
    )

    ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src/taiyidsl").is_dir())
    NOTEBOOK = ROOT / "docs/tutorials/ramsey_clock_transport.ipynb"
    OUTPUT = ROOT / "output/ramsey_clock_transport"
    OUTPUT.mkdir(parents=True, exist_ok=True)

    """)
    md(r"""
    ## 1. Device databases for two AWGs and a DDS card

    Retain the existing MOT, camera and A/B entries. Card C adds `dds_trigger_c` at illustrative RTIO channel 4. Use an actual free wired output at the laboratory. All three cards share a reference clock and have dedicated external triggers. The SIM serials and documentation core host are offline placeholders.
    """)
    source = original('"""OFFLINE example:')
    source += '\n\ndevice_db["dds_trigger_c"] = {\n    "type": "local",\n    "module": "artiq.coredevice.ttl",\n    "class": "TTLOut",\n    "arguments": {"channel": 4},\n}'
    code(source)
    code(
        original('"""Project configuration,')
        + """

spec_db["A"]["mode"] = "awg"
spec_db["B"]["mode"] = "awg"
spec_db["C"] = {
    "address": "/dev/spcm2",
    "serial_number": "SIM_C",
    "model": "M4i.6631-x8",
    "mode": "dds",
    "active_channels": [0, 1],
    "sample_rate_hz": 1_250_000_000,
    "reference_clock_hz": 125_000_000,
    "output_range_mv": 500,
    "dds_routing": "16/5",
    "dds_trigger_latency_ns": 560.0,  # Example only. Measure the delay for this card.
}
"""
    )
    md(r"""
    A and B declare delays of 500 and 520 samples (400 and 416 ns). C has an independent example delay of 560 ns. With a 1200 ns shared trigger lead, the prefixes are 800 ns on A, 784 ns on B and 640 ns on C. A and B play zeros during their prefixes. C holds nonzero tones. The 16/5 hardware routing puts X on CH0/core0 and Y on CH1/core8.
    """)
    md(r"""
    ## 2. Map the global field, interaction zone and AOD axes

    `nuclear_rf` retains `sharing="global"` even when only q0 is registered. B's clock/Rydberg coverage is iz0, so targets follow the atom's current position. C controls the two AOD axes independently of the spin and clock frames.

    Each shot starts the gate sequence with three TTLs at the same ARTIQ timestamp. A/B then follow AWG step tables and C follows its internal timer. Calibrate each trigger-to-output latency against the common reference. The local trigger that establishes C's initial hold runs at session initialization.
    """)
    code(
        original('"""Signal roles and physical wiring,')
        + """

descriptor["name"] = "clock_ramsey_transport_teaching_lab"
descriptor["instruments"]["C"] = {"kind": "spectrum", "device": "C"}
descriptor["channels"]["clock"]["coverage"] = {"zones": ["iz0"]}
descriptor["channels"]["rydberg"]["coverage"] = {"zones": ["iz0"]}
descriptor["channels"].update({
    "aod_x": {"instrument": "C", "channel": 0},
    "aod_y": {"instrument": "C", "channel": 1},
    "dds_trigger_c": {"instrument": "core", "device": "dds_trigger_c"},
})
descriptor["sync_groups"]["qubit_awg"].update({
    "cards": ["A", "B", "C"],
    "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b", "C": "dds_trigger_c"},
    "timing": {"trigger_lead_mu": 1200},
})
# The group keeps its original name. Each card declares its own mode.
setup = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
display(setup.connections())
display(setup.trigger_connections())
"""
    )
    md(r"""
    ## 3. LabConfig, q0 and transport calibration

    `LabConfig` combines the setup, registered qubit and transport declaration. q0 retains its RF/clock settings and gains a home position. The interaction zone has two sites. Only `iz0[0]` is occupied in this example. `SiteRef` carries the site's identity and coordinates, and `to=q0.position` requests a return home.
    """)
    source = original("q0 = Qubit(")
    if "position=None" not in source:
        raise ValueError("Base q0 position changed; review the transport derivation")
    code(
        source.replace(
            "position=None",
            'position=Position(0 * um, 0 * um, 0 * um, "tweezer_plane")',
        )
    )
    code("""
    iz0 = IZ("iz0", sites=(
        Position(30 * um, 10 * um, 0 * um, "tweezer_plane"),
        Position(36 * um, 10 * um, 0 * um, "tweezer_plane"),
    ))
    transport = TransportConfig(
        card="C",
        x_calibration=((0, 70e6), (40e-6, 90e6)),
        y_calibration=((0, 70e6), (15e-6, 85e6)),
        bindings={"q0": {"x_core": 0, "y_core": 8}},
        initial_amplitudes={0: 0.08, 8: 0.10},
        zones=(iz0,),
        position_tolerance=1e-9,
        frequency_tolerance_hz=1.0,
        min_separation=1e-6,
    )
    lab = LabConfig(setup=setup, qubits=[q0], transport=transport, emulation=True)
    initial_position = q0.position
    display([
        {"state": "home", "x_um": 0, "y_um": 0, "X_MHz": 70, "Y_MHz": 70},
        {"state": "iz0[0]", "x_um": 30, "y_um": 10, "X_MHz": 85, "Y_MHz": 80},
    ])
    """)
    md(r"""
    The linear calibration is `fX = 70 MHz + x × 0.5 MHz/µm` and `fY = 70 MHz + y × 1 MHz/µm`. X goes from 70 to 85 MHz and Y from 70 to 80 MHz on the outbound path. The return reverses them. The compiled values below include quantization. Normalized X/Y amplitudes stay at 0.08/0.10, with no position-dependent gain correction. Trap depth requires physical calibration.

    The transport snapshot binds geometry, calibration and cores. If two atoms share a Y tone, move all affected atoms together. In the two-atom three-card example, q0/q1 use X cores 0/1 and share Y core 8. Each batch pairs atoms with destinations by list order.
    """)
    md(r"""
    ## 4. Native preparation and readout

    Use the same native class and timing boundaries as plain Ramsey. The variable MOT prefix has unknown wall-clock duration. The fixed 15 ms + 16 ns tail and the 100.48 ms readout appear in the full pulse sheet. Once the first run establishes C's hold, it continues through preparation, readout and host waits.

    The fixture uses `NativeKernelPackage.from_package` because its initializer resets the core and its finalize method runs per shot. For independent components, `NativeModule.from_package` with `NativeProgram` puts reset under session ownership and runs finalize at session close. The camera trigger needs a separate adapter to obtain images. [Native components](../native_components.md) · [Laboratory extension](../../examples/lab_extension/README.md) · [Readout adapters](../readout_adapters.md)
    """)
    code(original("package = NativeKernelPackage.from_package("))
    md(r"""
    ## 5. Name the pulses and transport profile

    Use q0's π/2 RF settings and π clock settings, then reuse the same RF pulse twice. `MoveProfile.minimum_jerk(segments=32)` samples the requested normalized trajectory `s(u)=10u³−15u⁴+6u⁵`. Specify the duration for each move. The hardware uses a finite set of linear frequency ramps. The 100 µs ramp, 10.24 µs settle and 200 µs clock intervals align to 6.4 ns. Interior quantization is reported separately.
    """)
    code(
        original(
            "# After editing amplitude or duration, rebuild the pulse and experiment."
        )
        + """

transport_profile = MoveProfile.minimum_jerk(segments=32)
t_move = 100 * us
t_settle = 10.24 * us
"""
    )
    md(r"""
    ## 6. Build the outbound move, clock pulse and return

    The serial sequence is RF π/2, move to IZ, settle, clock 2π, return home, settle, RF π/2. Each move specifies its destination and occupies the ramp plus settling interval. The experiment function accepts preparation, readout and shots and returns an Experiment. Only the extra phase of the second RF pulse is swept.
    """)
    code("""
    def clock_ramsey_transport(q0, detuning, clock_pulse, pi2_pulse, phase, *,
                              interaction_site, move_duration, move_profile, settle,
                              prepare, readout, shots=2, preparation_to_gate=10 * us,
                              group="qubit_awg"):
        frequency = q0.parameters.rf.frequency * Hz + detuning
        exp = Experiment("clock_ramsey_transport", [q0], metadata={
            "hardware_verified": False,
            "transport_positions": "planned_not_camera_observed",
            "clock_2pi_assumption": "constant resonant Rabi rate; demo only",
        })
        with exp.average(shots):
            with exp.sweep(phase):
                with exp.realtime():
                    prepare()
                    delay(preparation_to_gate)
                    with exp.triggered_sequence(group=group):
                        reset_phase(q0.phase_frame("spin"))
                        reset_phase(q0.phase_frame("clock_control"))
                        play(q0.rf, pi2_pulse, frequency=frequency)
                        move(q0, to=interaction_site, duration=move_duration,
                             profile=move_profile, settle=settle)
                        play(q0.clock, clock_pulse)
                        move(q0, to=q0.position, duration=move_duration,
                             profile=move_profile, settle=settle)
                        play(q0.rf, pi2_pulse, frequency=frequency, phase=phase)
                    readout()
        return exp
    """)
    md(r"""
    The RF gap grows from 200 to 420.48 µs, and the second RF start moves from 300 to 520.48 µs. At 30 + 1 = 31 kHz the evolving RF reference gives the zero-offset point a phase of `(31 kHz × 520.48 µs × 360°) mod 360° = 48.5568°`. Add the requested phase on the other points. The clock has its own control frame. This calculation predicts output timing and phase; atom transport coherence requires its own measurement.
    """)
    code("""
    second_phases = SweepParameter("second_phase", [0, 45, 90, 135, 180], "deg")
    clock_exp = clock_ramsey_transport(
        q0=q0, detuning=1 * kHz, clock_pulse=clock_pulse, pi2_pulse=pi2_pulse,
        phase=second_phases, interaction_site=iz0[0],
        move_duration=t_move, move_profile=transport_profile, settle=t_settle,
        prepare=prepare_MOT, readout=readout, shots=2,
    )
    clock_compiled = lab.compile(clock_exp)
    assert q0.position == initial_position
    print("Scan points:", len(clock_compiled.points), "shots/point:", clock_compiled.shots)
    display(clock_compiled.axes)

    """)
    md(r"""
    `clock_compiled.export_report(...)` shows the compiled timing and waveforms. The following cell adds a source snapshot to the report for provenance. Its paths identify the experiment definition used to generate the report.
    """)
    code("""
    # Attach the original experiment source to the report.

    saved_notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    source_snapshot = OUTPUT / "notebook_code_snapshot.py"
    source_snapshot.write_text("\\n\\n".join(
        "# Notebook cell " + str(i) + "\\n" + "".join(c["source"])
        for i, c in enumerate(saved_notebook["cells"]) if c["cell_type"] == "code"
    ), encoding="utf-8")
    clock_report = clock_compiled.export_report(OUTPUT / "report", source_path=source_snapshot)
    display(Markdown(f"[Full compiled report]({clock_report['index'].resolve().as_uri()})"))
    """)
    md(r"""
    ## 7. Inspect how the compiler finds each starting position

    1. Each point starts from home with its initial tones. The compiler schedules the first move and settle and sets its internal q0 position to iz0[0].
    2. The clock play now addresses the atom in the interaction zone. Card B emits one shared field.
    3. The second move reads iz0[0] as its starting position and uses the original q0.position as its requested destination.
    4. The internal position returns home and the shot closure check succeeds. Each new point/shot starts at home.

    The table reads the compiled logical events. The external q0.position remains unchanged.
    """)
    code("""
    point = clock_compiled.points[0]
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    tick = clock_compiled.setup.tick_seconds
    moves = [e for e in point.logical_events if e.kind == "move"]
    display([{
        "move": i + 1,
        "start_us": (e.start_mu - region.start_mu) * tick * 1e6,
        "motion_end_us": (e.start_mu - region.start_mu + e.params["motion_duration_mu"]) * tick * 1e6,
        "settle_end_us": (e.end_mu - region.start_mu) * tick * 1e6,
        "from_um": [e.params["initial_layout"]["q0"][a] * 1e6 for a in ("x", "y")],
        "to_um": [e.params["final_layout"]["q0"][a] * 1e6 for a in ("x", "y")],
        "max_knot_time_error_ns": e.params["max_timing_quantization_error_s"] * 1e9,
    } for i, e in enumerate(moves)])
    assert moves[0].params["final_layout"] == moves[1].params["initial_layout"]
    assert moves[1].params["final_layout"] == moves[0].params["initial_layout"]
    assert q0.position == initial_position
    clock_event = next(e for e in point.logical_events if e.kind == "play" and e.signal == q0.clock)
    assert clock_event.params["operation_qubits"] == ["q0"]
    print("clock targets:", clock_event.params["operation_qubits"])
    """)
    md(r"""
    ## 8. Inspect the DDS command sequence on C

    The compiler maps trajectory knots to X/Y frequencies, quantizes frequency words and snaps times to the 6.4 ns grid. Each segment uses its quantized duration to compute `frequency_slope_hz_per_s`. Internal timer commands change the slope at intermediate points. At each endpoint a command pins the frequency, zeroes frequency/amplitude slopes and retains nonzero amplitude. Consecutive moves also account for the one-update latency of the pin. This example includes settle/clock intervals in which the hold is directly visible.

    C has its own command path and no AWG zero padding or absolute software phase ledger. Inspect the actual command frames below. The report also exports the complete JSON and CSV relative to the common gate entrance.
    """)
    code("""
    physical = next(e for e in point.physical_events if e.kind == "spectrum_program")
    cards = physical.params["cards"]
    dds = cards["C"]["program"]
    prefix_mu = dds["prefix_mu"]
    interesting = {0, len(dds["frames"]) - 1}
    interesting.update(i for i, f in enumerate(dds["frames"]) if f["set_frequency"])
    interesting.update(i for i, f in enumerate(dds["frames"]) if
        i and any(t["frequency_slope_hz_per_s"] != 0 for t in f["tones"])
        and all(t["frequency_slope_hz_per_s"] == 0 for t in dds["frames"][i-1]["tones"]))
    display([{
        "frame": i, "time_us": (f["at_mu"] - prefix_mu) * tick * 1e6,
        "next_trigger": f["trigger_source"], "pin_frequency": f["set_frequency"],
        "tones": [{"core": t["core"], "MHz": t["frequency_hz"] / 1e6,
                   "MHz_per_us": t["frequency_slope_hz_per_s"] / 1e12,
                   "amplitude": t["amplitude"]} for t in f["tones"]],
    } for i, f in enumerate(dds["frames"]) if i in interesting])
    print("Commands per shot:", dds["command_count"], "Resident commands including initialization and reserved setup:", dds["resident_command_count"], "/ 4096")
    print("Frequency quantization and slope error:")
    display(dds["quantization"])
    assert dds["initial_tones"] == dds["final_tones"]
    assert dds["frames"][-1]["trigger_source"] == "card"
    assert all(t["amplitude"] > 0 and t["frequency_slope_hz_per_s"] == 0
               and t["amplitude_slope_per_s"] == 0 for t in dds["frames"][-1]["tones"])
    """)
    md(r"""
    ## 9. Compare AWG output and DDS commands on the same axis

    `waveform.html` in the standard report displays A/B DAC output, C's predicted frequency in MHz and C's amplitude in full scale. `clock_compiled.waveform_simulator(path)` exports the same viewer. Zoom from int16 min/max bins to individual DAC samples. C's curves include quantized commands, slope changes, pin latency and nonzero holds. Absolute RF tone phase is outside the DDS model.

    The default view concatenates known Spectrum windows in execution order (5 points × 2 shots). Unknown host waits appear as boundaries. Advanced controls can focus on one execution or the known control window including C's preparation/readout hold. The second link focuses on point 0 relative to control T0. Relative to the gate entrance, external triggers occur at −1.2 µs, A/B sample 0 at −0.8/−0.784 µs and the first C frame at −0.64 µs. Use the pulse sheet for native timing and section 7 for planned positions.
    """)
    code("""
    waveform_url = clock_report["waveform"].resolve().as_uri()
    gate_start_s = region.start_mu * tick
    gate_end_s = region.end_mu * tick
    display(Markdown(f"[Waveform viewer for A/B/C across all points and shots]({waveform_url})"))
    detail_url = (
        f"{waveform_url}?point=0&shot=0"
        f"&t0={gate_start_s-2e-6:.12f}&t1={gate_end_s+2e-6:.12f}"
    )
    display(Markdown(f"[Inspect the full gate sequence for point 0]({detail_url})"))
    print("Gate entrance relative to control T0:", gate_start_s * 1e6, "μs")
    display(Markdown(f"[Full pulse sheet]({clock_report['pulse_sheet'].resolve().as_uri()})"))
    """)
    md(r"""
    | Time from gate entrance (µs) | A | B | C / position |
    |---|---|---|---|
    | 0–100 | RF π/2 | 0 | Home hold at 70/70 MHz |
    | 100–200 | 0 | 0 | Outbound, X 70→85 and Y 70→80 MHz |
    | 200–210.24 | 0 | 0 | IZ settle at 85/80 MHz |
    | 210.24–410.24 | 0 | Clock 2π | IZ hold at 85/80 MHz |
    | 410.24–510.24 | 0 | 0 | Return, X 85→70 and Y 80→70 MHz |
    | 510.24–520.48 | 0 | 0 | Home settle |
    | 520.48–620.48 | RF π/2 at scan phase | 0 | Home hold |
    | Readout and inter-shot | Finite playback ends | Finite playback ends | Home hold until session close |

    The assertions use the compiled events. Actual AWG trailing zeros can extend beyond 620.48 µs and leave all gate boundaries intact.
    """)
    code("""
    expected_starts = [0, 100, 210.24, 410.24, 520.48]
    for point in clock_compiled.points:
        region = next(e for e in point.logical_events if e.kind == "spectrum_region")
        operations = [e for e in point.logical_events if e.kind in {"play", "move"}]
        np.testing.assert_allclose([(e.start_mu-region.start_mu)*tick*1e6 for e in operations], expected_starts, rtol=0, atol=1e-9)
        np.testing.assert_allclose(region.duration_mu*tick*1e6, 620.48, rtol=0, atol=1e-9)
        for e in operations:
            if e.kind == "play" and e.signal == q0.rf:
                assert e.params["frequency"] == 31_000
        program = next(e for e in point.physical_events if e.kind == "spectrum_program").params["cards"]["C"]["program"]
        assert program["initial_tones"] == program["final_tones"]
    assert q0.position == initial_position
    print("Five phase points passed timing, 31 kHz RF, round-trip and position checks.")
    """)
    md(r"""
    ## 10. Hold the tweezer tones through readout and between shots

    Reaching the end of a ramp fixes its terminal frequency. In the IZ, C holds 85/80 MHz at 0.08/0.10 full scale through settling and clock illumination. Back home it holds 70/70 MHz. Setting the last frame's trigger source to CARD selects how a future command starts. The tone continues.

    On the first run the session configures and resets, establishes the initial hold, starts C once, waits for all cards to become ready and issues the common external trigger. Later shots preload SINGLE commands while holding, advance under the internal timer and return to the terminal hold. AWG bank switches, camera readout, host waits and return from `session.run()` preserve this hold. Check trigger-count increments and error status for completion. An empty FIFO alone cannot confirm execution of the terminal frame.

    Keep the session open across runs. Closing stops DDS; a failure stops the cards and marks position unknown. C maintains no absolute tone phase shared with A/B and issues no reset/phase jump while moving. The unknown inter-shot hold duration remains unspecified.
    """)
    md(r"""
    ## 11. Use one explicit hardware session

    Supply laboratory device databases, wiring, synchronization and AOD calibration, qubit parameters and native callbacks. Rebuild named pulses from calibrated_q0. This example is a Markdown listing kept outside Run All.

    ```python
    calibrated_pi2_pulse = Square(
        duration=calibrated_q0.parameters.rf.pi2_duration,
        amplitude=calibrated_q0.parameters.rf.pi2_amplitude,
    )
    calibrated_clock_pulse = Square(
        duration=2 * calibrated_q0.parameters.clock.pi_duration,
        amplitude=calibrated_q0.parameters.clock.pi_amplitude,
    )
    real_lab = LabConfig(
        setup=real_setup, qubits=[calibrated_q0], transport=real_transport,
        emulation=False, artiq_python=artiq_python,
    )
    real_exp = clock_ramsey_transport(
        q0=calibrated_q0, detuning=1 * kHz,
        clock_pulse=calibrated_clock_pulse, pi2_pulse=calibrated_pi2_pulse,
        phase=second_phases, interaction_site=real_iz[0],
        move_duration=t_move, move_profile=transport_profile, settle=t_settle,
        prepare=real_prepare, readout=real_readout, shots=100,
    )
    real_compiled = real_lab.compile(real_exp)
    with real_lab.connect(clock_session_ids=measured_clock_session_ids) as session:
        receipt = session.run(real_compiled)
        # Keep subsequent runs inside this context so card C maintains the trap.
    ```

    Connect verifies devices and clocks, then run uploads and executes. Supply the separate ARTIQ interpreter and current clock-session identity. Leaving the context stops DDS. After a fault the previous position is unknown and automatic transport retry is unavailable. The returned receipt records execution. Frame acquisition and data commit require a [readout adapter](../readout_adapters.md).
    """)
    md(r"""
    ## 12. Change settings and validate the result

    For amplitude or duration changes rebuild the named pulse, experiment and compile/report. For RF frequency or detuning rebuild the experiment and compile. For site or calibration changes rebuild transport and LabConfig. For trajectory or duration changes rebuild the profile or change the move arguments. Check the 6.4 ns DDS grid, frame-command budget and expected timings after any intentional change.

    A fresh notebook run exercises Python, compiled events, commands and packed A/B DAC. Browser interaction, real ARTIQ compilation, measured Spectrum outputs and atom survival have separate acceptance criteria. The example does not implement automatic routing or feedback. Moves are serial and cannot be nested in parallel/ASAP structure.

    [IZ and move](../transport_dds.md) · [Three-card round trip](../../examples/artiq_spectrum/transport_dds.py) · [Spectrum DDS mode](https://spectrum-instrumentation.com/download_files/application_notes/PN_Using_DDS_Mode_on_AWG.php) · [Complex ramps](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/12_dds_complex_ramps.py)
    """)
    for index, cell in enumerate(cells):
        cell.id = f"ramsey-transport-{index:03d}"
    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3 (project)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12.15"},
            "taiyidsl": {
                "base_notebook": BASE.name,
                "base_sha256": hashlib.sha256(BASE.read_bytes()).hexdigest(),
                "generator": "scripts/build_clock_transport_tutorial.py",
                "hardware_executed": False,
            },
        },
    )
    nbformat.validate(notebook)
    nbformat.write(notebook, DESTINATION)
    print(
        json.dumps(
            {
                "notebook": str(DESTINATION),
                "cells": len(cells),
                "code_cells": sum(c.cell_type == "code" for c in cells),
            },
            ensure_ascii=False,
        )
    )
    return DESTINATION


if __name__ == "__main__":
    build()
