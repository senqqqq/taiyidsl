"""Bounded AWG DAC replay and native DDS command curves for Spectrum.

This module neither opens a driver nor models the analog output chain or a
quantum state. Times are relative to the logical shot T0, including the declared
trigger latency and the compiled calibration prefix. A window never changes a
pulse's carrier phase or expands the entire experiment's idle intervals.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import zlib
from fractions import Fraction
from html import escape
from pathlib import Path

import numpy as np

from taiyidsl.backends.artiq_spectrum.awg import materialize_segment, reconstruct_window

from ..ir import CompileError
from ..serialization import to_jsonable
from .dds_view import dds_card_views as _dds_views
from .resources import asset_text

DEFAULT_MAX_SAMPLES = 100_000
DEFAULT_MAX_EMBEDDED_BYTES = 64 * 1024**2


def _fraction(value):
    return Fraction(str(value))


def _window(start_s, duration_s, max_samples):
    if (
        isinstance(start_s, bool)
        or isinstance(duration_s, bool)
        or not math.isfinite(start_s)
        or not math.isfinite(duration_s)
        or duration_s <= 0
    ):
        raise ValueError("Window start must be finite and duration must be positive")
    if type(max_samples) is not int or max_samples < 1:
        raise ValueError("max_samples must be a positive integer")
    return _fraction(start_s), _fraction(start_s) + _fraction(duration_s)


def _point(compiled, point):
    compiled.assert_integrity()
    if compiled.provenance.get("backend", {}).get("name") != "artiq_spectrum":
        raise CompileError("Output simulator currently supports artiq_spectrum only")
    if type(point) is not int or not 0 <= point < len(compiled.points):
        raise IndexError("Output simulator point is outside the compiled scan")
    return compiled.points[point]


def _card_views(compiled, point):
    for event in point.physical_events:
        if event.kind != "spectrum_program":
            continue
        group = event.params
        trigger = group["trigger_physical_mu"] * _fraction(
            group["trigger_time_unit_s"]
        ) - point.origin_mu * _fraction(compiled.setup.tick_seconds)
        for name, card in group["cards"].items():
            contract, program = card["contract"], card["program"]
            if program.get("kind") == "dds":
                # DDS has commands, not captured/final packed DAC samples.
                continue
            rate = _fraction(program["card_spec"]["sample_rate_hz"])
            sample0 = trigger + contract["trigger_latency_samples"] / rate
            yield {
                "group": group["name"],
                "region_id": group["region_id"],
                "card": name,
                "contract": contract,
                "program": program,
                "sample0": sample0,
                "trigger_time_s": float(trigger),
                "calibration_status": group["calibration"].get("status", "unmeasured"),
            }


def simulate_output(
    compiled, point=0, start_s=0.0, duration_s=1e-6, *, max_samples=DEFAULT_MAX_SAMPLES
):
    """Return exact final DAC codes in a half-open [start, start+duration) window.

    ``point`` is the zero-based compiled-point position. ``max_samples`` bounds
    the requested sample count per channel, even if a program covers only part
    of that window. Overlarge windows are rejected; no RF downsampling occurs.

    ``traces`` contains one entry per active card channel and triggered region.
    NumPy arrays ``sample_indices``, ``time_s``, ``dac_codes`` (little-endian
    int16), ``normalized`` and ``voltage_v`` have equal lengths. Samples outside
    a finite program are omitted, rather than invented. Voltage is the nominal
    configured range times code/32767, not a calibrated analog prediction.
    Resolved pulse recipes are metadata; phase is not inferred from real RF.
    A point containing only delays or frame updates returns an empty trace list.
    """
    selected = _point(compiled, point)
    begin, end = _window(start_s, duration_s, max_samples)
    traces = []
    for view in _card_views(compiled, selected):
        program, contract = view["program"], view["contract"]
        rate = _fraction(program["card_spec"]["sample_rate_hz"])
        left = math.ceil((begin - view["sample0"]) * rate)
        right = math.ceil((end - view["sample0"]) * rate)
        if right - left > max_samples:
            raise ValueError(
                f"Requested window needs {right - left} samples/channel; "
                f"max_samples={max_samples}. Choose a shorter window."
            )
        size = program["physical_duration_samples"]
        left, right = max(0, min(size, left)), max(0, min(size, right))
        codes = reconstruct_window(program, left, right, max_samples=max_samples)
        indices = np.arange(left, right, dtype=np.int64)
        times = float(view["sample0"]) + indices / float(rate)
        for column, channel in enumerate(program["card_spec"]["active_channels"]):
            values = codes[:, column].copy()
            normalized = values.astype(np.float64) / 32767
            traces.append(
                {
                    "group": view["group"],
                    "region_id": view["region_id"],
                    "card": view["card"],
                    "channel": channel,
                    "sample_rate_hz": float(rate),
                    "sample0_time_s": float(view["sample0"]),
                    "trigger_time_s": view["trigger_time_s"],
                    "trigger_latency_samples": contract["trigger_latency_samples"],
                    "calibration_status": view["calibration_status"],
                    "output_range_mv": contract["output_range_mv"],
                    "physical_duration_samples": size,
                    "sample_indices": indices.copy(),
                    "time_s": times.copy(),
                    "dac_codes": values,
                    "normalized": normalized,
                    "voltage_v": normalized * contract["output_range_mv"] / 1000,
                    "pulses": copy.deepcopy(
                        [p for p in program["pulses"] if p["channel"] == channel]
                    ),
                }
            )
    from .native_view import native_lifecycle

    lifecycle = native_lifecycle(compiled)
    result = {
        "name": compiled.name,
        "point": point,
        "coordinates": copy.deepcopy(selected.coordinates),
        "start_s": float(begin),
        "duration_s": float(end - begin),
        "max_samples": max_samples,
        "time_reference": (
            lifecycle["time_reference"] if lifecycle else "shot_logical_T0"
        ),
        "model": "ideal_DAC_reconstruction",
        "analog_chain_simulated": False,
        "traces": traces,
    }
    if lifecycle:
        result["native_lifecycle"] = lifecycle["points"][point]
        result["full_shot_duration_s"] = None
        result["native_waveforms_simulated"] = False
    return result


def _payload(compiled, *, max_embedded_bytes=DEFAULT_MAX_EMBEDDED_BYTES):
    """Lossless resident bytes; zero loops stay symbolic and hashes deduplicate."""
    _point(compiled, 0)
    if type(max_embedded_bytes) is not int or max_embedded_bytes < 1:
        raise ValueError("max_embedded_bytes must be a positive integer")
    unique, total = set(), 0
    # A conservative preflight precedes materialization or compression. An
    # explicitly zero recipe consumes no payload; other recipes count even if
    # a later quantization happens to make every code zero.
    for point in compiled.points:
        for view in _card_views(compiled, point):
            program = view["program"]
            width = len(program["card_spec"]["active_channels"])
            for segment in program["segments"]:
                digest = segment["sha256"]
                if digest in unique:
                    continue
                unique.add(digest)
                if segment["recipe"]["kind"] != "zero":
                    total += segment["sample_count"] * width * 2
                if total > max_embedded_bytes:
                    raise CompileError(
                        "Output simulator unique nonzero-recipe resident data "
                        f"needs at least {total} bytes; max_embedded_bytes="
                        f"{max_embedded_bytes}. Export fewer scan points or "
                        "separate point viewers, or explicitly increase this budget."
                    )
    from .viewer_execution import execution_axis

    axis = execution_axis(compiled)
    control_windows = {point["index"]: point["control"] for point in axis["points"]}
    points, packed = [], {}
    for index, point in enumerate(compiled.points):
        views = []
        for view in _card_views(compiled, point):
            program = view["program"]
            segments = []
            for segment in program["segments"]:
                item = {k: segment[k] for k in ("id", "sample_count", "sha256")}
                digest = item["sha256"]
                if digest not in packed:
                    data = materialize_segment(program, segment["id"])
                    raw = data.tobytes(order="C")
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise CompileError("Output simulator segment hash mismatch")
                    packed[digest] = {
                        "encoding": "zero" if not np.any(data) else "zlib-base64",
                        "byte_length": len(raw),
                        "data": (
                            ""
                            if not np.any(data)
                            else base64.b64encode(zlib.compress(raw)).decode("ascii")
                        ),
                    }
                segments.append(item)
            views.append(
                {
                    "group": view["group"],
                    "region_id": view["region_id"],
                    "card": view["card"],
                    "sample0_time_s": float(view["sample0"]),
                    "sample0_time_ns": float(view["sample0"] * 10**9),
                    "sample0_fraction": [
                        str(view["sample0"].numerator),
                        str(view["sample0"].denominator),
                    ],
                    "trigger_time_s": view["trigger_time_s"],
                    "sample_rate_hz": program["card_spec"]["sample_rate_hz"],
                    "active_channels": program["card_spec"]["active_channels"],
                    "output_range_mv": view["contract"]["output_range_mv"],
                    "trigger_latency_samples": view["contract"][
                        "trigger_latency_samples"
                    ],
                    "calibration_status": view["calibration_status"],
                    "physical_duration_samples": program["physical_duration_samples"],
                    "pulses": program["pulses"],
                    "segments": segments,
                    "steps": program["steps"],
                }
            )
        points.append(
            {
                "index": index,
                "coordinates": point.coordinates,
                "cards": views,
                "dds": list(
                    _dds_views(
                        compiled, point, control_window=control_windows[point.index]
                    )
                ),
            }
        )
    result = {
        "name": compiled.name,
        "schema_version": 1,
        "max_samples": DEFAULT_MAX_SAMPLES,
        "max_embedded_bytes": max_embedded_bytes,
        "unique_resident_bytes_upper_bound": total,
        "points": points,
        "packed": packed,
    }
    result["execution_axis"] = axis
    from .native_view import native_lifecycle

    lifecycle = native_lifecycle(compiled)
    if lifecycle:
        result["time_reference"] = lifecycle["time_reference"]
        result["native_lifecycle"] = lifecycle
        result["native_waveforms_simulated"] = False
    return to_jsonable(result)


def write_output_simulator(
    compiled, path=None, *, max_embedded_bytes=DEFAULT_MAX_EMBEDDED_BYTES
):
    """Return self-contained offline HTML, or write it and return ``Path``.

    The browser reads losslessly compressed final AWG DAC segments and native
    DDS frequency/amplitude command curves on one shared time axis. DDS curves
    include slope quantization and hold, without inventing sampled RF or phase.
    Resident segments are deduplicated
    across scan points. Large resident waveform sets produce correspondingly
    large HTML files; long zero waits remain compact. Modern browsers supporting
    ``DecompressionStream('deflate')`` are required; no network or SDK is used.

    The default shows all card channels and consecutive executions (up to fifty
    per page), ordered by the compiled averaging/chunk plan. Only known Spectrum
    windows are concatenated; native preparation/readout and unknown host waits
    are omitted at marked boundaries. Known DDS hold spans these fixed windows;
    unknown shot gaps remain unmeasured. Advanced controls can select full known
    control windows or local detail. This is not a measured wall-clock axis.

    Links select ``point``, ``shot``, ``group``, ``card``, ``channel``,
    ``logical_id``, ``t0`` and ``t1`` (seconds relative to shot T0) in local detail
    mode without hiding the other channels. The clickable legend controls
    visibility across zooms. Windows exceeding the sample budget use labelled
    exact min/max DAC bins; zoom switches to individual samples. Display offsets
    do not enter sample decoding or rephase repeated shots.

    ``max_embedded_bytes`` defaults to 64 MiB of unique raw resident bytes across
    points, before compression; symbolic-zero recipes are exempt. Preflight
    counts nonzero recipes conservatively even if quantization later yields
    all-zero codes. Exceeding this budget raises before sample materialization,
    never truncates the data. Plotly and JSON metadata are additional overhead.
    """
    from plotly.offline import get_plotlyjs

    payload = _payload(compiled, max_embedded_bytes=max_embedded_bytes)
    encoded = (
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    script = (
        asset_text("viewer_execution.js") + "\n" + asset_text("output_simulator.js")
    )
    from .native_view import native_lifecycle_html

    native_panel = native_lifecycle_html(compiled)
    replacements = {
        "TITLE": escape(compiled.name),
        "PLOTLY": get_plotlyjs(),
        "PAYLOAD": encoded,
        "SCRIPT": script,
        "NATIVE_LIFECYCLE": native_panel,
        "TIME_REFERENCE": "control epoch T0" if native_panel else "logical shot T0",
    }
    html = re.sub(
        r"__(TITLE|PLOTLY|PAYLOAD|SCRIPT|NATIVE_LIFECYCLE|TIME_REFERENCE)__",
        lambda match: replacements[match[1]],
        _HTML,
    )
    if path is None:
        return html
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Output simulator</title>
<style>
:root{--paper:#f4f2eb;--panel:#fffef9;--ink:#252b2a;--muted:#68716c;
--line:#d9ddd3;--teal:#167c79;--orange:#b45c32}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:14px "Segoe UI",Arial,sans-serif}main{max-width:1600px;margin:auto;padding:32px}
header{display:flex;gap:24px;justify-content:space-between;align-items:flex-start;
border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:24px}
.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:11px;color:var(--teal);
font-weight:700}h1{font:500 36px Georgia,serif;margin:9px 0}h2{font-size:16px;margin:0}
.subtitle{color:var(--muted);line-height:1.6;max-width:920px;margin:0}.badge{
border:1px solid var(--teal);border-radius:30px;padding:8px 12px;color:var(--teal);
white-space:nowrap;font-size:12px}.controls{display:grid;grid-template-columns:
repeat(4,minmax(110px,1fr));gap:16px;background:var(--panel);
border:1px solid var(--line);
padding:20px}.controls label{display:flex;flex-direction:column;gap:7px;
font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
select,input,button{font:14px "Segoe UI",Arial,sans-serif;border:1px solid var(--line);
border-radius:4px;padding:10px;background:#fff;color:var(--ink);min-width:0}
button{cursor:pointer}button:hover{border-color:var(--teal)}button.primary{
background:var(--teal);color:white;border-color:var(--teal)}.wide{grid-column:span 2}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0}
.toolbar span{flex:1;color:var(--muted)}
.actions{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.actions button{flex:1}
#notice{min-height:22px;margin:14px 0;color:var(--muted);line-height:1.5}
#notice.error{color:#9c402a}#notice.warning{color:#86541e}.plot-card{background:
var(--panel);border:1px solid var(--line);padding:20px 14px 8px}.plot-heading{
display:flex;align-items:baseline;justify-content:space-between;padding:0 12px;
gap:12px;flex-wrap:wrap}#stats{font:12px Consolas,monospace;color:var(--muted)}
#waveform{height:560px}.footer{margin-top:20px;display:grid;grid-template-columns:
1fr 1fr;gap:24px;color:var(--muted);font-size:12px;line-height:1.7}.footer strong{
color:var(--ink);font-weight:600}.inspection{margin-top:20px;
border:1px solid var(--line);
background:var(--panel)}summary{cursor:pointer;padding:14px 18px}.table-scroll{
max-height:300px;overflow:auto;padding:0 18px 16px}table{border-collapse:collapse;
width:100%;font:12px Consolas,monospace}th,td{text-align:right;padding:7px 12px;
border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:var(--panel);
color:var(--muted)}#sample-note{padding:0 18px;color:var(--muted);font-size:12px}
#metadata{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}a{color:var(--teal)}
#intervals{color:var(--muted);line-height:1.5;margin:12px 0 0}
button:disabled{opacity:.45;cursor:default}
@media(max-width:800px){main{padding:18px}.controls{grid-template-columns:1fr 1fr}
h1{font-size:28px}.footer{grid-template-columns:1fr}header{flex-direction:column}
#waveform{height:420px}.badge{align-self:flex-start}}
</style><script>__PLOTLY__</script></head><body><main>
<header><div><div class="eyebrow">TaiyiDSL / Waveform inspection</div>
<h1>Output simulator</h1><p class="subtitle" id="experiment"></p>
<p class="subtitle">Ideal DAC reconstruction for AWG and native Spectrum DDS
frequency/amplitude commands · all channels and repeated shots on one time axis
</p></div>
<span class="badge">OFFLINE · SPECTRUM</span></header>
<div class="toolbar"><button id="full-sequence">Fit sequence</button>
<span id="execution-summary"></span>
<button id="page-previous" hidden>Previous executions</button>
<button id="page-next" hidden>Next executions</button></div>
<p id="notice" role="status" aria-live="polite"></p>
<details class="inspection" id="advanced"><summary>Advanced · selection, time window and
pulse inspection</summary>
<section class="controls" aria-label="Advanced waveform controls">
<label class="wide">Executions<select id="selection-mode"></select></label>
<label class="wide">Time axis<select id="projection"></select></label>
<label>First execution
<input id="execution-start" type="number" min="0" value="0"></label>
<label>Maximum shown
<input id="execution-count" type="number" min="1" max="500" value="50"></label>
<label>Point to inspect<select id="point"></select></label>
<label>Shot for local detail
<input id="shot-index" type="number" min="0" value="0"></label>
<label>Window start (µs)<input id="start" type="number" step="any"></label>
<label>Window length (µs)<input id="duration" type="number" step="any" min="0"></label>
<label>AWG vertical axis<select id="units"><option value="codes">DAC code</option>
<option value="voltage">Nominal voltage (V)</option><option value="normalized">
Normalized full scale</option></select></label>
<label>AWG sample display<select id="display">
<option value="lines">Connected samples</option>
<option value="markers">Sample points</option>
<option value="both">Line + points</option>
<option value="hold">Zero-order hold guide</option></select></label>
<div class="actions wide">
<button class="primary" id="render">Apply window</button></div>
<p class="wide">The selectors below inspect pulse or DDS tone metadata;
all channels stay on the plot.
Use the legend to show or hide a channel.</p>
<label>Inspect trigger group<select id="group"></select></label>
<label>Inspect card<select id="card"></select></label>
<label>Inspect channel<select id="channel"></select></label>
<label class="wide">Compiled pulse<select id="pulse"></select></label>
<div class="actions wide"><button id="focus-pulse">Zoom to pulse</button>
<button id="pulse-edge">Zoom to leading edge</button></div>
</section><p id="intervals"></p><pre id="metadata"></pre></details>
<section class="plot-card"><div class="plot-heading">
<h2 id="trace-title">All cards and channels</h2>
<span id="stats"></span></div><div id="waveform"></div></section>
<details class="inspection"><summary>Inspect sample grid and exact DAC codes</summary>
<p id="sample-note"></p><div class="table-scroll"><table><thead><tr>
<th>Card sample</th><th>Local T0 (ns)</th><th>DAC code</th><th>Nominal V</th>
</tr></thead><tbody id="samples"></tbody></table></div></details>
__NATIVE_LIFECYCLE__
<section class="footer"><div><strong>Reading this view</strong><br>
All active cards and channels appear together; click a legend item to show or hide it.
The default concatenates Spectrum gate windows in execution order, including every
pulse, move, delay between regions, compensation prefix and physical tail. Dotted
boundaries omit other preparation/readout stages and unknown host waits;
this is not elapsed wall-clock time.
Advanced can show full known control windows or a single local shot.<br>
Each shot replays its compiled bytes. Display offsets never change the carrier phase.
Long windows show exact min/max DAC bins; zoom loads individual samples.
No RF is regenerated, and samples outside finite AWG programs remain undefined.
Native Spectrum DDS tones appear in separate frequency (MHz) and amplitude (FS)
panels on the same time axis. These curves predict quantized DDS commands, including
endpoint holds; they are not DAC samples, calibrated trap depths or measured motion.
The AWG unit selector does not change DDS units. The session holds DDS output through
omitted shot/host gaps, whose durations are not inferred here.<br>
<a href="pulse_sheet.html">Open the experiment pulse sheet →</a></div>
<div><strong>Scope</strong><br>
This is not an analog-chain or quantum-state simulation. Nominal voltage uses the
configured full-scale range. Timing declarations are not hardware validation.
ARTIQ DDS/TTL outputs remain in the pulse sheet. Within each shot, all cards use the
same __TIME_REFERENCE__; declared trigger latency and calibration
prefixes are already included.
The overview is paged only when more than 50 executions exist; Advanced can change
its bounded range without duplicating resident waveform data.</div></section>
</main><script id="output-data" type="application/json">__PAYLOAD__</script>
<script>__SCRIPT__</script></body></html>
"""
