"""Local-file compatible, on-demand replay of compiled chunks and DAC segments."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import zlib
from html import escape
from pathlib import Path

import numpy as np

from taiyidsl.backends.artiq_spectrum.awg import materialize_segment

from .._artifact_io import atomic_json
from ..compiler.store import ArtifactSegments
from .gate_annotations import point_gate_annotations
from .output_simulator import _annotated_pulses, _card_views, _dds_views
from .resources import asset_bytes
from .viewer_execution import execution_axis


def _javascript(value):
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def export_store_report(store, directory, *, captures=()):
    """Write a portable offline directory; no fetch, web server or RF synthesis.

    Metadata loads one chunk at a time. Packed DAC segments load only when a
    requested window intersects them. The decoder retains its bounded LRU;
    compressed payload references are released after reconstruction.
    """
    from plotly.offline import get_plotlyjs

    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    data_dir = output / "data"
    data_dir.mkdir(exist_ok=True)
    (output / "plotly.js").write_text(get_plotlyjs(), encoding="utf-8")
    (output / "decoder.js").write_bytes(asset_bytes("output_simulator.js"))
    (output / "paged.js").write_bytes(asset_bytes("paged_output.js"))
    written, summaries = set(), []
    samples = ArtifactSegments(store) if "segments" in store.manifest else None
    for chunk_info in store.manifest["chunks"]:
        compiled = store.load_chunk(chunk_info["index"])
        windows = {
            point["index"]: point["control"]
            for point in execution_axis(compiled)["points"]
        }
        points = []
        for local, point in enumerate(compiled.points):
            labels, virtual_z = point_gate_annotations(compiled, point)
            cards = []
            for view in _card_views(compiled, point):
                program = view["program"]
                segments = []
                for segment in program["segments"]:
                    digest = segment["sha256"]
                    item = {
                        key: segment[key] for key in ("id", "sample_count", "sha256")
                    }
                    item["byte_length"] = (
                        segment["sample_count"]
                        * len(program["card_spec"]["active_channels"])
                        * 2
                    )
                    segments.append(item)
                    if digest not in written:
                        values = (
                            samples.load(program, segment["id"])
                            if samples is not None
                            else materialize_segment(program, segment["id"])
                        )
                        raw = values.tobytes(order="C")
                        if hashlib.sha256(raw).hexdigest() != digest:
                            raise ValueError("Paged viewer segment hash mismatch")
                        packed = {
                            "byte_length": len(raw),
                            "encoding": "zero",
                            "data": "",
                        }
                        if np.any(values):
                            packed.update(
                                encoding="zlib-base64",
                                data=base64.b64encode(zlib.compress(raw)).decode(
                                    "ascii"
                                ),
                            )
                        (data_dir / f"segment_{digest}.js").write_text(
                            f"TaiyiPaged.register('segment:{digest}',{_javascript(packed)});\n",
                            encoding="utf-8",
                        )
                        written.add(digest)
                cards.append(
                    {
                        "group": view["group"],
                        "card": view["card"],
                        "sample0_time_s": float(view["sample0"]),
                        "sample0_fraction": [
                            str(view["sample0"].numerator),
                            str(view["sample0"].denominator),
                        ],
                        "sample_rate_hz": program["card_spec"]["sample_rate_hz"],
                        "active_channels": program["card_spec"]["active_channels"],
                        "output_range_mv": view["contract"]["output_range_mv"],
                        "physical_duration_samples": program[
                            "physical_duration_samples"
                        ],
                        "segments": segments,
                        "steps": program["steps"],
                        "pulses": _annotated_pulses(program["pulses"], labels),
                    }
                )
            points.append(
                {
                    "index": chunk_info["points"][local],
                    "coordinates": point.coordinates,
                    "cards": cards,
                    "virtual_z": virtual_z,
                    "dds": list(
                        _dds_views(compiled, point, control_window=windows[point.index])
                    ),
                }
            )
        index = chunk_info["index"]
        payload = _javascript({"points": points})
        (data_dir / f"chunk_{index}.js").write_text(
            f"TaiyiPaged.register('chunk:{index}',{payload});\n",
            encoding="utf-8",
        )
        code_dir = output / f"chunk_{index}"
        code_dir.mkdir(exist_ok=True)
        source_dir = store.directory / chunk_info["directory"]
        for name, source in (
            ("manifest.json", source_dir / "manifest.json"),
            ("experiment.py", source_dir / "artiq" / "experiment.py"),
        ):
            shutil.copyfile(source, code_dir / name)
        compiled.visualize(code_dir / "pulse_sheet.html")
        summaries.append(
            {
                "index": index,
                "points": chunk_info["points"],
                "managed_bytes": chunk_info["managed_bytes"],
                "boundary_reason": chunk_info["boundary_reason"],
            }
        )
        del compiled, points
    capture_links = []
    for index, capture in enumerate(captures):
        destination = output / f"capture_{index}.html"
        shutil.copyfile(capture, destination)
        capture_links.append(
            f'<a href="{destination.name}">Capture comparison {index}</a>'
        )
    report = {
        "schema_version": 1,
        "artifact_sha256": store.sha256,
        "host_execution_plan": store.host_execution_plan,
        "chunks": summaries,
        "sample_source": "verified_final_packed_segments",
        "dds_source": "quantized_command_prediction_no_RF_phase",
        "hardware_status": "not_verified",
        "analog_chain_simulated": False,
    }
    atomic_json(output / "report.json", report)
    links = "\n".join(
        f"<li>Chunk {c['index']}: points {c['points'][0]}–{c['points'][-1]} · "
        f'<a href="chunk_{c["index"]}/pulse_sheet.html">Pulse sheet</a> · '
        f'<a href="chunk_{c["index"]}/manifest.json">Actual bank / entries</a> · '
        f'<a href="chunk_{c["index"]}/experiment.py">ARTIQ source</a></li>'
        for c in summaries
    )
    html = (
        _HTML.replace("__TITLE__", escape(store.manifest["name"]))
        .replace("__PLAN__", _javascript(summaries))
        .replace("__LINKS__", links)
        .replace("__CAPTURES__", " · ".join(capture_links))
    )
    (output / "index.html").write_text(html, encoding="utf-8")
    return {"index": output / "index.html", "report": output / "report.json"}


_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · compiled AWG / DDS</title>
<style>body{font:16px system-ui;margin:24px auto;max-width:1400px;
padding:0 20px;color:#203438;background:#f5f6f3}
header{border-bottom:1px solid #cbd2cf;padding-bottom:16px}h1{font-size:26px}
.controls{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0}
label{display:flex;flex-direction:column;gap:6px;font-size:13px}
input,select,button{padding:9px;border:1px solid #a6b6ae;border-radius:4px;
background:white;max-width:280px}
button{cursor:pointer;background:#176e67;color:white}a{color:#176e67}#notice{min-height:28px;color:#805015}
#wave{height:720px;background:white}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
li{margin:8px 0}</style>
<header><h1>__TITLE__</h1><p>Final packed AWG DAC and native DDS command curves ·
load one chunk and requested segments only.
All card channels share one time axis. No instrument access;
nominal voltage and DDS curves are not measured waveforms.</p></header>
<div class="controls"><label>Chunk<select id="chunk"></select></label>
<label>Global point<select id="point"></select></label>
<label>Inspect card / channel<select id="channel"></select></label>
<label>AWG color<select id="color-by"><option value="pulse">By physical pulse</option>
<option value="channel">By channel</option></select></label>
<label>Start from logical T0 (µs)<input id="start" value="0"></label>
<label>Window (µs)<input id="duration" value="1"></label>
<button id="render">View window</button>
<button id="full-sequence">Full sequence</button></div>
<p id="notice"></p><div id="wave"></div>
<details><summary>Selected pulse / reference metadata</summary>
<pre id="metadata"></pre></details>
<h2>Compiled artifacts</h2><ul>__LINKS__</ul><p>__CAPTURES__</p>
<p>Long AWG windows show exact min/max of final DAC samples; zoom for individual
samples. DDS frequency (MHz) and amplitude (FS) remain command predictions with
no RF phase reconstruction. Nonzero DDS hold extends through known control
intervals; host waits and shot gaps have no assigned duration.
AWG samples outside finite programs are omitted.
AWG pulse colors are stable across zooms; gray marks samples outside pulse intervals.
The legend hides the entire channel in either color mode.
Chunk switching does not reset the compiled frame.
Files work offline via local classic scripts; keep this directory together.</p>
<script id="paged-plan" type="application/json">__PLAN__</script>
<script src="plotly.js"></script><script src="decoder.js"></script>
<script src="paged.js"></script></html>"""
