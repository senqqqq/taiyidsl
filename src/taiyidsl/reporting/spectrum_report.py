"""Offline code views of the real Spectrum adapter and resolved upload arguments.

No SDK import or instrument access is needed. Source snapshots are evidence of
the implementation at export time, not a record of a hardware run.
"""

from __future__ import annotations

import ast
import hashlib
import html

from ..config.spectrum import INTERNAL_PLL_BENCH
from .resources import RUNTIME_SOURCES, runtime_source_bytes


def card_execution(item):
    """Resolve the manifest exactly as SpcmAdapter.upload maps segment slots."""
    contract, program = item["contract"], item["program"]
    if program.get("kind") in {"dds", "dds_bank"}:
        from .spectrum_dds_report import dds_execution

        return dds_execution(item)
    slots = {segment["id"]: slot for slot, segment in enumerate(program["segments"])}
    configuration = [
        {"setting": name, "value": value, "source": source}
        for name, value, source in (
            ("serial_number", contract["serial_number"], "contract"),
            (
                "clock.mode",
                "SPC_CM_INTPLL"
                if contract.get("clock_policy") == INTERNAL_PLL_BENCH
                else "SPC_CM_EXTREFCLOCK",
                "adapter; checked each shot",
            ),
            ("clock.reference_clock [Hz]", contract["reference_clock_hz"], "contract"),
            ("clock.sample_rate [Hz]", contract["sample_rate_hz"], "contract"),
            ("card_mode", "SPC_REP_STD_SEQUENCE", "adapter"),
            ("active_channels", contract["active_channels"], "contract"),
            ("output_range [mV]", contract["output_range_mv"], "contract"),
            ("output offset / stop level", "0 / SPCM_STOPLVL_ZERO", "adapter"),
            ("ARTIQ trigger_device", contract["trigger_device"], "contract"),
            ("trigger.or_mask / and_mask", "SPC_TMASK_EXT0 / 0", "adapter"),
            ("trigger.ext0_mode / coupling", "SPC_TM_POS / COUPLING_DC", "adapter"),
            (
                "trigger.ext0_level0 [mV]",
                contract.get("trigger_level_mv", 1000),
                "contract" if "trigger_level_mv" in contract else "adapter default",
            ),
            (
                "trigger termination [ohm]",
                contract.get("trigger_termination_ohm", 50),
                "contract"
                if "trigger_termination_ohm" in contract
                else "adapter default",
            ),
            ("trigger.delay [samples]", 0, "adapter; programmable extra delay"),
            (
                "trigger_latency_samples",
                contract["trigger_latency_samples"],
                "contract timing model; NOT trigger.delay",
            ),
        )
    ]
    uploads = [
        {
            "segment_id": segment["id"],
            "hardware_slot": slots[segment["id"]],
            "samples_per_channel": segment["sample_count"],
            "sha256": segment["sha256"],
        }
        for segment in program["segments"]
    ]
    steps = [
        {
            "step_index": step["index"],
            "next_step_index": step["next_step"],
            "segment_index": slots[step["segment_id"]],
            "loops": step["loops"],
            "flags": "SPCSEQ_END" if step["end"] else "SPCSEQ_ENDLOOPALWAYS",
        }
        for step in program["steps"]
    ]
    # This is a review listing, never executed or substituted for the adapter.
    lines = [
        "# Resolved upload arguments; inspection only, NOT a standalone runner.",
        "# Runtime guards and SDK readbacks: see the exact adapter source above.",
        f"sequence.max_segments({program['memory']['segment_slots']!r})",
        "",
        "# 1. Waveform DMA: loader validates/materializes the manifest recipes.",
        "# Report .bin files are review copies, not the runner's input files.",
    ]
    for upload in uploads:
        lines += [
            f"# segment ID {upload['segment_id']} -> hardware slot "
            f"{upload['hardware_slot']}",
            f"data = segment_loader(program, {upload['segment_id']!r})",
            f"sequence.write_segment({upload['hardware_slot']!r})",
            f"sequence.segment_size({upload['samples_per_channel']!r})",
            "transfer = sdk.DataTransfer(card)",
            f"transfer.allocate_buffer({upload['samples_per_channel']!r})",
            "transfer.buffer[:] = data.T",
            "transfer.start_buffer_transfer(",
            "    sdk.M2CMD_DATA_STARTDMA, sdk.M2CMD_DATA_WAITDMA)",
            "",
        ]
    lines.append("# 2. Static step table: no per-gate external trigger wait.")
    for step in steps:
        lines += [
            f"sequence.step_memory({step['step_index']!r},",
            f"    next_step_index={step['next_step_index']!r}, "
            f"segment_index={step['segment_index']!r},",
            f"    loops={step['loops']!r}, flags=sdk.{step['flags']})",
        ]
    lines += [
        "sequence.start_step(0)  # Select entry step; does NOT start RF playback.",
        "card.write_setup()",
        "# 3. Runner arms only AFTER every participating card has been uploaded.",
    ]
    if program.get("kind") == "spectrum_bank":
        lines += [
            "# Before each shot, select its finite entry without uploading again:",
            *(
                f"# point {entry['point']}: adapter.select_entry({entry['entry_step']})"
                for entry in program["entries"]
            ),
        ]
    return {
        "configuration": configuration,
        "uploads": uploads,
        "step_writes": steps,
        "upload_listing": "\n".join(lines) + "\n",
    }


_METHODS = (
    ("SpcmAdapter", "configure_for_calibration"),
    ("SpectrumRunner", "run_point"),
    ("SpectrumRunner", "run_resident_point"),
    ("SpectrumRunner", "_load_bank"),
    ("SpcmAdapter", "configure"),
    ("SpcmAdapter", "upload"),
    ("SpcmAdapter", "select_entry"),
    ("SpcmAdapter", "arm"),
    ("SpcmAdapter", "ready"),
    ("SpcmAdapter", "wait_complete"),
)


def export_execution_sources(output):
    """Copy exact runtime bytes and record source ranges without importing it."""
    directory = output / "runtime"
    directory.mkdir(exist_ok=True)
    snapshots = {}
    for entry in RUNTIME_SOURCES:
        content = runtime_source_bytes(entry)
        (output / entry.report_path).write_bytes(content)
        snapshots[entry.key] = content
    runtime = snapshots["spectrum_runtime"]
    tree = ast.parse(runtime.decode("utf-8"))
    methods = {}
    for class_name, method_name in _METHODS:
        cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
        )
        method = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == method_name
        )
        methods[f"{class_name}.{method_name}"] = {
            "start_line": method.lineno,
            "end_line": method.end_lineno,
        }
    sources = {}
    for key, relative in (
        ("spectrum_runtime", "runtime/spectrum_runtime.py"),
        ("host_bridge", "export/experiment.py"),
        ("artiq_kernel", "export/artiq/experiment.py"),
    ):
        content = (output / relative).read_bytes()
        sources[key] = {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    for entry in RUNTIME_SOURCES:
        if entry.key == "spectrum_runtime":
            continue
        content = snapshots[entry.key]
        sources[entry.key] = {
            "path": entry.report_path,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return {
        "status": "code_preview_not_execution",
        "sources": sources,
        "runtime_methods": methods,
    }


def _source_html(execution, output, key, method=None):
    record = execution["sources"][key]
    source = (output / record["path"]).read_text(encoding="utf-8")
    label = record["path"]
    if method:
        span = execution["runtime_methods"][method]
        source = "\n".join(
            source.splitlines()[span["start_line"] - 1 : span["end_line"]]
        )
        label += f" · lines {span['start_line']}–{span['end_line']}"
    return (
        f'<p class="muted"><a href="{record["path"]}">{html.escape(label)}</a>'
        f"<br>Source SHA-256: <code>{record['sha256']}</code></p>"
        f'<pre class="source"><code>{html.escape(source)}</code></pre>'
    )


def execution_html(execution, output):
    """One-page call order, actual shared adapter code, and exported kernels."""

    def method_block(title, *names, opened=False):
        return (
            f"<details {'open' if opened else ''}><summary>{title}</summary>"
            + "".join(
                _source_html(execution, output, "spectrum_runtime", name)
                for name in names
            )
            + "</details>"
        )

    return "\n".join(
        [
            '<section id="execution"><h2>ARTIQ + Spectrum execution</h2>',
            "<p>The export includes a snapshot of the runtime adapters and the host "
            "and kernel source generated for this experiment. The compiled point "
            "tables show each card, setting and resolved SDK argument.</p>",
            "<h3>Clock initialization and recalibration</h3>",
            "<p><code>configure_for_calibration()</code> explicitly selects the "
            "external reference and sample rate, starts a new clock session and "
            "invalidates earlier phase calibration. The default synchronized path "
            "requires valid measured calibration. The single-AWG internal PLL bench "
            "path does not call this method: it verifies the internal clock, plays "
            "uncorrected setpoints and does not verify analog delay, gain, or "
            "absolute phase relative to ARTIQ.</p>",
            method_block(
                "Explicit clock initialization source",
                "SpcmAdapter.configure_for_calibration",
            ),
            "<p>The exported <code>GeneratedExperiment.run()</code> checks the ARTIQ "
            "target and calls the <code>initialize_devices()</code> kernel once. "
            "It then enters the shot loop in host_execution_plan averaging and chunk "
            "order. The initialization kernel and each point kernel appear below.</p>",
            "<h3>Shots with Spectrum output</h3>",
            '<ol class="execution-flow">'
            "<li><b>Prepare the host.</b> "
            "<code>result_adapter.before_shot(point, shot)</code></li>"
            "<li><b>Check every card.</b> Probe identity, capacity, clock and current "
            "calibration before configuring any card.</li>"
            "<li><b>Configure and upload on bank changes.</b> "
            "A.configure → A.upload → B.configure → B.upload. Configuration checks "
            "clocks and sets the outputs and EXT0. Upload writes shared samples "
            "and the full step table. With a resident bank, select the finite "
            "entry for this shot directly.</li>"
            "<li><b>Arm all cards.</b> A.arm → B.arm → wait until all are armed. "
            "AWG playback awaits the trigger while DDS trap output holds.</li>"
            '<li class="kernel-step"><b>Run one ARTIQ point kernel.</b> '
            "The host callback <code>_execute_point(point)</code> dispatches to "
            "<code>point_N()</code>. Following preparation, the scheduled start "
            "TTLs launch the cards together. Spectrum plays the static sequence "
            "autonomously through gates, delays and virtual Z. ARTIQ performs "
            "readout on the compiled timeline.</li>"
            "<li><b>Check completion.</b> After the kernel returns, the runner "
            "waits for every card, calls "
            "<code>result_adapter.after_shot(point, shot)</code> and records the "
            "receipt.</li></ol>",
            "<p>A and B illustrate a two-card setup. The manifest lists the cards "
            "at each point. Schema 2 reuses resident AWG banks. Schema 3 also "
            "contains per-shot DDS commands. Global averaging preserves the sampling "
            "order and can reload a prior bank. Chunk averaging finishes a chunk "
            "before switching. Schema 1 uploads each point. Points without Spectrum "
            "work go straight to ARTIQ and omit the automatic start TTL. The runner "
            "enters the kernel only after every card is armed and stops any cards "
            "it touched on failure.</p>",
            method_block(
                "Host orchestration and readiness checks",
                "SpectrumRunner.run_point",
                "SpectrumRunner.run_resident_point",
                "SpectrumRunner._load_bank",
            ),
            '<h3 id="spectrum-code">Spectrum configuration and playback source</h3>',
            "<details><summary>DDS20 trap hold, command compiler and adapter</summary>"
            "<p>DDS cards start during session initialization and hold trap output "
            "between shots and across AWG bank changes. Each shot appends a prepared "
            "command packet, starts on an external trigger, executes timed commands "
            "and returns to hold. The AWG segment, zero-tail and re-arm steps below "
            "apply to AWG cards. The DDS plot predicts commands. It carries neither "
            "a software absolute phase nor a final DAC trace.</p>",
            _source_html(execution, output, "spectrum_dds_runtime"),
            _source_html(execution, output, "spectrum_dds"),
            "</details>",
            method_block(
                "1 · Output and EXT0 configuration with per-setting readback",
                "SpcmAdapter.configure",
            ),
            "<p>EXT0 starts on a rising edge. Defaults are 1000 mV, 50 Ω and DC "
            "coupling. Each card lists the values and their sources. The SDK call "
            "<code>trigger.delay(0)</code> sets the additional programmable delay. "
            "The compiled <code>trigger_latency_samples</code> models the physical "
            "trigger-to-sample-0 delay.</p>",
            method_block(
                "2 · Waveform DMA upload and 3 · step table write", "SpcmAdapter.upload"
            ),
            "<p>SegmentCache renders and verifies manifest recipes before DMA. "
            "<code>data.T</code> matches the SDK buffer's channel dimension. "
            "The .bin files are inspection copies. Upload order in the segments "
            "list assigns hardware slots independently of logical segment IDs. "
            "<code>SPCSEQ_ENDLOOPALWAYS</code> advances after the finite loop. "
            "The last step uses <code>SPCSEQ_END</code>. Playback through gates "
            "proceeds without additional triggers. <code>start_step</code> selects "
            "the entry. Each point in a bank ends independently.</p>",
            method_block(
                "Per-shot entry selection for a resident bank",
                "SpcmAdapter.select_entry",
            ),
            method_block(
                "4 · Arm, check readiness and verify completion",
                "SpcmAdapter.arm",
                "SpcmAdapter.ready",
                "SpcmAdapter.wait_complete",
            ),
            "<p><code>arm()</code> uses <code>M2CMD_CARD_ENABLETRIGGER</code> and "
            "waits for the external TTL. The SDK <code>M2STAT_CARD_READY</code> "
            "indicates stopped/completed status. The adapter's <code>ready()</code> "
            "source shows how it determines that a card awaits a trigger.</p>",
            '<h3 id="artiq-code">Generated host bridge and ARTIQ kernel</h3>',
            "<details><summary>Complete host bridge using SpectrumRunner</summary>",
            _source_html(execution, output, "host_bridge"),
            "</details><details><summary>Complete exported ARTIQ program and "
            "per-point trigger timing</summary>",
            _source_html(execution, output, "artiq_kernel"),
            "</details></section>",
        ]
    )
