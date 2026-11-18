"""Final packed DAC replay, bounded windows, browser/Python byte parity."""

import copy
import json
import math
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    ns,
    rz,
    trigplay,
)
from taiyidsl.ir import CompileError
from taiyidsl.output_simulator import (
    _payload,
    simulate_output,
    write_output_simulator,
)
from taiyidsl.spectrum_waveforms import build_card_program, reconstruct_window

JS_PATH = (
    Path(__file__).parents[1] / "src/taiyidsl/reporting/assets/output_simulator.js"
)


def _pulse(**values):
    return dict(
        {
            "channel": 0,
            "start_sample": 140,
            "duration_samples": 500,
            "frequency_hz": 150e6,
            "amplitude": 0.61,
            "phase_rad": 0.24,
            "envelope": {"kind": "square"},
            "logical_id": ["e1"],
        },
        **values,
    )


def _compiled(program, *, name="Packed DAC example"):
    contract = dict(
        program["card_spec"], output_range_mv=500, trigger_latency_samples=500
    )
    event = SimpleNamespace(
        kind="spectrum_program",
        params={
            "name": "qubit_awg",
            "region_id": "r0",
            "trigger_physical_mu": 3488,
            "trigger_time_unit_s": 1e-9,
            "calibration": {"status": "unmeasured"},
            "cards": {"A": {"program": program, "contract": contract}},
        },
    )
    point = SimpleNamespace(
        index=0, origin_mu=20000, coordinates={}, physical_events=[event]
    )
    return SimpleNamespace(
        name=name,
        provenance={"backend": {"name": "artiq_spectrum"}},
        points=[point],
        setup=SimpleNamespace(tick_seconds=0.2e-9),
        assert_integrity=lambda: None,
    )


def _program(pulses=None, duration=2000, **kwargs):
    return build_card_program(
        {"active_channels": [0, 1]},
        pulses if pulses is not None else [_pulse()],
        duration,
        **kwargs,
    )


def _node(payload, windows):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required to test browser decoder")
    script = """
const fs = require('node:fs');
const api = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const result = [];
  for (const window of input.windows) {
    const view = input.payload.points[window.point || 0].cards[window.card || 0];
    let options = {...window};
    if (window.sequence) Object.assign(options,
      api.sequenceWindow(input.payload.points[window.point || 0].cards));
    if (window.input_us) Object.assign(options, api.inputWindow(...window.input_us));
    if (window.query_seconds) {
      const query = api.queryWindow(...window.query_seconds);
      const fields = api.formatWindow(query.start_s, query.duration_s);
      Object.assign(options, api.inputWindow(fields.start_us, fields.duration_us));
    }
    if (window.pulse_index !== undefined) {
      const pulse = api.pulseWindow(view, view.pulses[window.pulse_index]);
      const fields = api.formatWindow(pulse.start_s, pulse.duration_s);
      Object.assign(options, api.inputWindow(fields.start_us, fields.duration_us));
    }
    if (window.preview_index !== undefined) {
      const preview = api.previewWindow(
        view, view.pulses[window.preview_index], 100000
      );
      const fields = api.formatWindow(preview.start_s, preview.duration_s);
      Object.assign(options, api.inputWindow(fields.start_us, fields.duration_us));
    }
    const decoder = window.overview ? api.overviewWindow : api.decodeWindow;
    const data = await decoder(view, input.payload.packed, options);
    result.push(Object.fromEntries(Object.entries(data).map(([k, v]) =>
       [k, typeof v === 'number' || typeof v === 'boolean' ? v : Array.from(v)])));
  }
  process.stdout.write(JSON.stringify(result));
})().catch(error => {console.error(error.message); process.exit(1);});
"""
    return subprocess.run(
        [node, "-e", script, str(JS_PATH)],
        input=json.dumps({"payload": payload, "windows": windows}),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_time_origin_includes_trigger_latency_prefix_and_physical_zero_tail():
    program = _program()
    compiled = _compiled(program)
    result = simulate_output(compiled, start_s=-112e-9, duration_s=1600e-9)
    first = result["traces"][0]
    assert first["sample0_time_s"] == pytest.approx(-112e-9, abs=1e-20)
    assert first["trigger_time_s"] == pytest.approx(-512e-9, abs=1e-20)
    assert first["sample_indices"][140] == 140
    assert first["time_s"][140] == pytest.approx(0, abs=1e-20)
    np.testing.assert_array_equal(
        first["dac_codes"], reconstruct_window(program, 0, 2000)[:, 0]
    )
    assert result["traces"][1]["channel"] == 1
    assert not result["traces"][1]["dac_codes"].any()
    assert first["dac_codes"].dtype == np.dtype("<i2")
    np.testing.assert_array_equal(first["voltage_v"], first["normalized"] * 0.5)
    assert result["time_reference"] == "shot_logical_T0"
    assert not result["analog_chain_simulated"]


def test_half_open_non_grid_window_and_finite_program_coverage():
    compiled = _compiled(_program())
    result = simulate_output(compiled, start_s=0.1e-9, duration_s=2e-9)
    np.testing.assert_array_equal(result["traces"][0]["sample_indices"], [141, 142])
    outside = simulate_output(compiled, start_s=1, duration_s=1e-6)
    assert all(len(trace["dac_codes"]) == 0 for trace in outside["traces"])
    end = simulate_output(compiled, start_s=1400e-9, duration_s=1e-6)
    assert end["traces"][0]["sample_indices"][-1] == 2015
    assert end["traces"][0]["dac_codes"][-1] == 0


@pytest.mark.parametrize(
    "start,duration,limit",
    [
        (0, 0, 10),
        (0, -1, 10),
        (0, math.inf, 10),
        (math.nan, 1, 10),
        (0, 1, 0),
        (0, 1, True),
    ],
)
def test_invalid_windows_rejected(start, duration, limit):
    with pytest.raises(ValueError):
        simulate_output(
            _compiled(_program()), start_s=start, duration_s=duration, max_samples=limit
        )


def test_large_requested_window_rejected_before_clipping_to_short_program():
    with pytest.raises(ValueError, match="shorter window"):
        simulate_output(_compiled(_program()), duration_s=0.1)


def test_frame_only_point_has_empty_trace_list_and_valid_offline_page():
    compiled = _compiled(_program())
    compiled.points[0].physical_events = []
    assert simulate_output(compiled)["traces"] == []
    assert _payload(compiled)["points"][0]["cards"] == []
    html = write_output_simulator(compiled)
    assert "This scan point contains no Spectrum AWG program" in html


def test_native_artiq_requires_supported_backend():
    compiled = _compiled(_program())
    compiled.provenance["backend"]["name"] = "artiq"
    with pytest.raises(CompileError, match="artiq_spectrum only"):
        simulate_output(compiled)


def test_html_export_checks_actual_packed_hash():
    program = _program()
    program["segments"][0]["sha256"] = "wrong"
    with pytest.raises(CompileError, match="hash mismatch"):
        write_output_simulator(_compiled(program))


def test_payload_budget_fails_before_materialization_and_deduplicates(monkeypatch):
    compiled = _compiled(_program())
    compiled.points.append(copy.deepcopy(compiled.points[0]))
    payload = _payload(compiled)
    budget = payload["unique_resident_bytes_upper_bound"]
    assert budget > 0
    assert _payload(compiled, max_embedded_bytes=budget)["packed"] == payload["packed"]

    def unexpected(*args):
        raise AssertionError("Must reject before materializing segments")

    monkeypatch.setattr("taiyidsl.output_simulator.materialize_segment", unexpected)
    with pytest.raises(CompileError, match="fewer scan points"):
        write_output_simulator(compiled, max_embedded_bytes=budget - 1)


def test_long_symbolic_zero_wait_does_not_exhaust_payload_budget():
    compiled = _compiled(_program([], duration=125_000_000))
    payload = _payload(compiled, max_embedded_bytes=1)
    assert payload["unique_resident_bytes_upper_bound"] == 0
    assert all(item["encoding"] == "zero" for item in payload["packed"].values())


def test_browser_microsecond_query_and_pulse_roundtrips_keep_exact_sample_edges():
    starts = [24640, 49140, 80640, 85640]
    program = _program([_pulse(start_sample=start) for start in starts], 100000)
    payload = _payload(_compiled(program))
    windows = [
        {"channel": 0, "input_us": [text, "0.08"]}
        for text in ("19.6000000000000", "39.2", "64.4", "68.4")
    ]
    windows += [
        {"channel": 0, "query_seconds": ["0.0000196", "0.00001968"]},
        {"channel": 0, "pulse_index": 0},
        # This decimal is just after the grid edge. It must NOT be rounded down
        # by an intermediate Number conversion before exact-ceil selection.
        {
            "channel": 0,
            "query_seconds": [
                "0.000019600000000000000000001",
                "0.000019680000000000000000001",
            ],
        },
    ]
    process = _node(payload, windows)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert [entry["first"] for entry in result] == starts + [24640, 24640, 24641]
    assert [len(entry["codes"]) for entry in result] == [100] * 5 + [500, 100]


def test_node_control_input_and_delayed_plot_commits_keep_latest_selection():
    """DOM/Plotly stubs test interaction logic, not browser rendering quality."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    compiled = _compiled(_program([_pulse(start_sample=24640)], duration=100000))
    empty = copy.deepcopy(compiled.points[0])
    empty.index, empty.physical_events = 1, []
    third = copy.deepcopy(compiled.points[0])
    third.index = 2
    cards = third.physical_events[0].params["cards"]
    cards["B"] = cards.pop("A")
    cards["B"]["contract"]["output_range_mv"] = 1000
    compiled.points.extend([empty, third])
    payload = _payload(compiled)
    script = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const selects = new Set([
  'point', 'group', 'card', 'channel', 'pulse', 'units', 'display',
  'selection-mode', 'projection'
]);
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.value = ''; this.textContent = '';
    this.listeners = {}; this.className = '';
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node.tag === 'fragment') {this.append(...node.children); continue;}
      this.children.push(node);
      if (this.tag === 'select' && this.children.length === 1) {
        this.value = String(node.value);
      }
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    if (this.tag === 'select') this.value = '';
    this.append(...nodes);
  }
  addEventListener(name, callback) {(this.listeners[name] ||= []).push(callback);}
  on(name, callback) {this.addEventListener(name, callback);}
  emit(name) {for (const callback of this.listeners[name] || []) callback({});}
}
const elements = new Map();
function get(id) {
  if (!elements.has(id)) {
    elements.set(id, new Element(selects.has(id) ? 'select' : 'div'));
  }
  return elements.get(id);
}
get('output-data').textContent = JSON.stringify(payload);
get('units').value = 'codes'; get('display').value = 'lines';
global.document = {getElementById: get, createElement: tag => new Element(tag),
  createDocumentFragment: () => new Element('fragment')};
global.location = {search: '?point=0&t0=0.0000196&t1=0.00001968&color_by=channel'};
const pending = [];
let outstanding = 0, maxOutstanding = 0;
global.Plotly = {react: (id, traces) => new Promise(resolve => {
  outstanding++; maxOutstanding = Math.max(maxOutstanding, outstanding);
  pending.push({traces, finish: () => {outstanding--; resolve();}});
})};
require(require('node:path').join(
  require('node:path').dirname(process.argv[1]), 'viewer_execution.js'));
require(process.argv[1]);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush();
  assert.equal(pending.length, 1);
  const fromQuery = pending[0].traces[0].customdata[0][4];
  pending.shift().finish(); await flush();
  get('start').value = '19.6000000000000'; get('duration').value = '0.08';
  get('render').emit('click'); await flush();
  const fromInput = pending[0].traces[0].customdata[0][4];
  pending.shift().finish(); await flush();
  assert.match(get('trace-title').textContent, /All 4 card channels/);
  // A delayed active-card plot must not commit its table after switching empty.
  get('point').value = '2'; get('point').emit('change'); await flush();
  assert.equal(pending.length, 1);
  get('point').value = '1'; get('point').emit('change'); await flush();
  assert.equal(pending.length, 1);
  pending.shift().finish(); await flush();
  assert.equal(pending.length, 1);
  assert(pending[0].traces.every(trace => trace.x.length === 0));
  pending.shift().finish(); await flush();
  assert.match(get('notice').textContent, /no Spectrum AWG program/);
  // A delayed empty plot must not overwrite a newer active-card selection.
  get('render').emit('click'); await flush();
  get('point').value = '0'; get('point').emit('change'); await flush();
  pending.shift().finish(); await flush();
  assert.equal(pending.length, 1);
  pending.shift().finish(); await flush();
  assert.match(get('trace-title').textContent, /All 4 card channels/);
  assert.notEqual(get('notice').className, 'error');
  assert.match(get('stats').textContent, /min\/max bins/);
  process.stdout.write(JSON.stringify({fromQuery, fromInput, maxOutstanding}));
})().catch(error => {console.error(error.stack); process.exit(1);});
"""
    process = subprocess.run(
        [node, "-e", script, str(JS_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "fromQuery": 24640,
        "fromInput": 24640,
        "maxOutstanding": 1,
    }


def _separated_three_channel_payload():
    duration = 650140
    nuclear = _pulse(
        start_sample=25140,
        duration_samples=250000,
        frequency_hz=30000,
        envelope={
            "kind": "flat_top_gaussian",
            "sigma_samples": 12500,
            "cutoff_sigma": 4,
            "flat_samples": 150000,
        },
    )
    clock = _pulse(
        start_sample=275140,
        duration_samples=375000,
        envelope={
            "kind": "flat_top_gaussian",
            "sigma_samples": 18750,
            "cutoff_sigma": 4,
            "flat_samples": 225000,
        },
        phase_modulation={"kind": "half_sine", "amplitude": math.pi},
    )
    rydberg = _pulse(
        channel=1,
        start_sample=400140,
        frequency_hz=200e6,
        envelope={
            "kind": "flat_top_gaussian",
            "sigma_samples": 25,
            "cutoff_sigma": 4,
            "flat_samples": 300,
        },
        phase_modulation={
            "kind": "polynomial",
            "coefficients": [0, 0, 3 * math.pi, -2 * math.pi],
        },
    )
    a = build_card_program({"active_channels": [0]}, [nuclear], duration)
    b = _program([clock, rydberg], duration)
    compiled = _compiled(a)
    compiled.points[0].physical_events[0].params["cards"]["B"] = {
        "program": b,
        "contract": dict(
            b["card_spec"], output_range_mv=500, trigger_latency_samples=500
        ),
    }
    return _payload(compiled)


def test_preview_windows_center_long_gaussians_and_keep_short_gate_complete():
    payload = _separated_three_channel_payload()
    windows = [
        {"card": 0, "channel": 0, "preview_index": 0},
        {"card": 1, "channel": 0, "preview_index": 0},
        {"card": 1, "channel": 1, "preview_index": 1},
    ]
    process = _node(payload, windows)
    assert process.returncode == 0, process.stderr
    nuclear, clock, rydberg = json.loads(process.stdout)
    assert nuclear["first"] == 108473
    assert len(nuclear["codes"]) == 83334
    assert clock["first"] == 462590
    assert len(clock["codes"]) == 100
    assert rydberg["first"] == 400140
    assert len(rydberg["codes"]) == 500
    assert all(
        max(abs(code) for code in trace["codes"]) > 10000
        for trace in (nuclear, clock, rydberg)
    )


def test_node_full_sequence_zoom_channel_switch_and_explicit_pulse_focus():
    """All-channel execution replay and UI state; not rendered browser QA."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    payload = _separated_three_channel_payload()
    second = copy.deepcopy(payload["points"][0])
    second["index"], second["coordinates"] = 1, {"demo": 1}
    payload["points"].append(second)
    axis = payload["execution_axis"]
    second_axis = copy.deepcopy(axis["points"][0])
    second_axis["index"] = 1
    axis["points"].append(second_axis)
    axis.update(shots=2, total_executions=4)
    script = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const selects = new Set(['point','group','card','channel','pulse','units',
  'display','selection-mode','projection']);
class Element {
  constructor(tag) { this.tag=tag; this.value=''; this.children=[];
    this.listeners={}; this.textContent=''; this.className=''; }
  append(...nodes) { for (const node of nodes) {
    if (node.tag==='fragment') {this.append(...node.children); continue;}
    this.children.push(node);
    if (this.tag==='select' && this.children.length===1) this.value=String(node.value);
  }}
  replaceChildren() {this.children=[]; if(this.tag==='select') this.value='';}
  addEventListener(name,fn) {(this.listeners[name] ||= []).push(fn);}
  on(name,fn) {this.addEventListener(name,fn);}
  emit(name,event={}) {for(const fn of this.listeners[name] || []) fn(event);}
}
const elements=new Map();
function get(id) {
  if(!elements.has(id)) elements.set(id,new Element(selects.has(id)?'select':'div'));
  return elements.get(id);
}
get('output-data').textContent=JSON.stringify(payload);
get('units').value='codes'; get('display').value='lines';
global.document={getElementById:get,createElement:tag=>new Element(tag),
  createDocumentFragment:()=>new Element('fragment')};
global.location={search:'?color_by=channel'};
let traces, layout, plots=0;
global.Plotly={react:async(id,data,next)=>{traces=data; layout=next; plots++;},
  restyle:(id,patch,indices)=>indices.forEach(i=>Object.assign(traces[i],patch))};
require(require('node:path').join(
  require('node:path').dirname(process.argv[1]),'viewer_execution.js'));
require(process.argv[1]);
async function plotted(previous) {
  for(let n=0;n<1000 && plots<=previous;n++)
    await new Promise(resolve=>setTimeout(resolve,1));
  assert(plots>previous,get('notice').textContent);
}
const rows=trace=>trace.customdata.filter(Boolean);
(async()=>{
  await plotted(0);
  assert.equal(get('selection-mode').value,'all');
  assert.equal(traces.length,3);
  assert.equal(new Set(traces.map(trace=>trace.line.color)).size,3);
  assert(traces.every(trace=>trace.showlegend && trace.visible===true));
  assert.equal(layout.legend.itemdoubleclick,false);
  assert.match(get('stats').textContent,/min\/max bins/);
  const order=[...new Set(rows(traces[0]).map(
    row=>JSON.stringify(row.slice(0,3))))].map(JSON.parse);
  assert.deepEqual(order,[[0,0,0],[1,1,0],[2,0,1],[3,1,1]]);
  assert(Number(get('duration').value)>2080);
  const originalCodes=rows(traces[0]).filter(row=>row[0]===0).map(row=>row[5]);
  const repeatedCodes=rows(traces[0]).filter(row=>row[0]===2).map(row=>row[5]);
  assert.deepEqual(repeatedCodes,originalCodes); // Repetition does not rephase DAC.
  get('waveform').emit('plotly_legendclick',{curveNumber:1});
  assert.equal(traces[1].visible,'legendonly');
  const offset=ViewerExecution.selectExecutions(
    payload.execution_axis,{projection:'awg'}).entries[0].offset_s*1e6;
  let before=plots;
  get('waveform').emit('plotly_relayout',{
    'xaxis.range[0]':220+offset,'xaxis.range[1]':220.08+offset});
  await plotted(before);
  assert.equal(traces.length,3);
  assert(traces.every(trace=>rows(trace).length===100));
  assert.equal(rows(traces[1])[0][4],275140);
  assert.equal(traces[1].visible,'legendonly'); // Visibility survives reconstruction.
  assert.equal(get('samples').children.length,100);
  get('waveform').emit('plotly_legendclick',{curveNumber:1});
  assert.equal(traces[1].visible,true);
  // Inspection selectors do not remove other channels or reset the viewport.
  before=plots;
  get('card').value='B'; get('card').emit('change');
  get('channel').value='1'; get('channel').emit('change');
  assert.equal(plots,before);
  get('focus-pulse').emit('click'); await plotted(before);
  assert.equal(get('selection-mode').value,'detail');
  assert.equal(get('start').value,'320'); assert.equal(get('duration').value,'0.4');
  assert.equal(traces.length,3);
  assert.equal(rows(traces[2]).length,500);
  assert(rows(traces[2]).filter(row=>row[5]!==0).length>490);
  assert(rows(traces[1]).some(row=>row[5]!==0)); // Concurrent clock remains visible.
  before=plots; get('selection-mode').value='all';
  get('selection-mode').emit('change'); await plotted(before);
  assert(Number(get('duration').value)>2080);
  assert.match(get('notice').textContent,/not measured wall-clock/);
  process.stdout.write(JSON.stringify({
    channels:traces.length,executions:order.length,shortGateSamples:500}));
})().catch(error=>{console.error(error.stack);process.exit(1);});
"""
    process = subprocess.run(
        [node, "-e", script, str(JS_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "channels": 3,
        "executions": 4,
        "shortGateSamples": 500,
    }


def test_overview_covers_all_pulses_gaps_and_symbolic_loops_from_final_bytes():
    program = _program(
        [_pulse(), _pulse(start_sample=1200, phase_rad=1.1)], duration=2000
    )
    payload = _payload(_compiled(program))
    # Annotation changes must not alter the plotted final DAC values.
    payload["points"][0]["cards"][0]["pulses"] = []
    process = _node(
        payload, [dict(channel=0, sequence=True, overview=True, max_bins=37)]
    )
    assert process.returncode == 0, process.stderr
    overview = json.loads(process.stdout)[0]
    size = program["physical_duration_samples"]
    actual = reconstruct_window(program, 0, size)[:, 0]
    assert overview["first"] == 0 and overview["last"] == size
    assert len(overview["starts"]) == 37
    assert overview["stops"][:-1] == overview["starts"][1:]
    for first, last, low, high in zip(
        overview["starts"],
        overview["stops"],
        overview["minimum"],
        overview["maximum"],
        strict=True,
    ):
        assert low == actual[first:last].min()
        assert high == actual[first:last].max()
    # Two nonzero groups stay separated by genuine zero time on the same axis.
    active = np.flatnonzero(
        np.asarray(overview["maximum"]) - np.asarray(overview["minimum"])
    )
    assert np.any(np.diff(active) > 1)

    late = 125_000_140
    long_program = _program([_pulse(), _pulse(start_sample=late)], duration=late + 500)
    process = _node(
        _payload(_compiled(long_program)),
        [dict(channel=0, sequence=True, overview=True, max_bins=37)],
    )
    assert process.returncode == 0, process.stderr
    overview = json.loads(process.stdout)[0]
    assert overview["last"] == long_program["physical_duration_samples"]
    assert len(overview["starts"]) == 37
    assert overview["maximum"][0] > 10000
    assert overview["maximum"][-1] > 10000
    assert overview["minimum"][1:-1] == overview["maximum"][1:-1] == [0] * 35


@pytest.mark.parametrize(
    "pulse",
    [
        _pulse(),
        _pulse(phase_rad=-math.pi / 2, frequency_hz=30000),
        _pulse(
            envelope={
                "kind": "flat_top_gaussian",
                "sigma_samples": 25,
                "cutoff_sigma": 4,
                "flat_samples": 300,
            },
            phase_modulation={"kind": "half_sine", "amplitude": math.pi},
        ),
        _pulse(
            envelope={
                "kind": "flat_top_gaussian",
                "sigma_samples": 25,
                "cutoff_sigma": 4,
                "flat_samples": 300,
            },
            phase_modulation={
                "kind": "polynomial",
                "coefficients": [0, 0, 3 * math.pi, -2 * math.pi],
            },
            phase_steps=[
                {"sample": 120, "delta_rad": 0.2},
                {"sample": 330, "delta_rad": -0.4},
            ],
        ),
    ],
    ids=["square", "resolved_virtual_z", "gaussian_half_sine", "cubic_phase_steps"],
)
def test_browser_replays_identical_final_quantized_samples_across_segments(pulse):
    program = _program([pulse], max_tile_samples=384)
    compiled = _compiled(program)
    payload = _payload(compiled)
    windows = [
        dict(start_s=-112e-9, duration_s=1e-6, channel=channel) for channel in (0, 1)
    ]
    process = _node(payload, windows)
    assert process.returncode == 0, process.stderr
    browser = json.loads(process.stdout)
    python = simulate_output(compiled, start_s=-112e-9, duration_s=1e-6)
    for actual, expected in zip(browser, python["traces"], strict=True):
        np.testing.assert_array_equal(actual["codes"], expected["dac_codes"])
        np.testing.assert_array_equal(actual["indices"], expected["sample_indices"])
        np.testing.assert_array_equal(actual["times"], expected["time_s"])
    assert len(set(browser[0]["segmentIds"])) > 1


def test_100ms_delay_is_symbolic_and_late_window_is_exact():
    start = 125_000_140
    program = _program(
        [_pulse(), _pulse(start_sample=start, phase_rad=0.75)], duration=start + 500
    )
    compiled = _compiled(program)
    payload = _payload(compiled)
    assert any(step["loops"] > 10000 for step in program["steps"])
    assert len(json.dumps(payload)) < 30000
    assert any(item["encoding"] == "zero" for item in payload["packed"].values())
    windows = [
        dict(start_s=0.0999999, duration_s=600e-9, channel=0),
        dict(start_s=0.05, duration_s=100e-9, channel=0),
    ]
    process = _node(payload, windows)
    assert process.returncode == 0, process.stderr
    for actual, window in zip(json.loads(process.stdout), windows, strict=True):
        expected = simulate_output(
            compiled, start_s=window["start_s"], duration_s=window["duration_s"]
        )["traces"][0]
        np.testing.assert_array_equal(actual["codes"], expected["dac_codes"])
        np.testing.assert_array_equal(actual["indices"], expected["sample_indices"])


def test_metadata_is_not_used_as_sample_source_and_bytes_deduplicate_across_points():
    program = _program()
    expected = reconstruct_window(program, 0, 2000)
    program["pulses"][0]["amplitude"] = (
        0  # Metadata deliberately disagrees with recipe.
    )
    compiled = _compiled(program)
    compiled.points.append(copy.deepcopy(compiled.points[0]))
    payload = _payload(compiled)
    assert len(payload["packed"]) == len({s["sha256"] for s in program["segments"]})
    process = _node(
        payload, [dict(point=1, channel=0, start_s=-112e-9, duration_s=1600e-9)]
    )
    assert process.returncode == 0, process.stderr
    np.testing.assert_array_equal(
        json.loads(process.stdout)[0]["codes"], expected[:, 0]
    )


def test_browser_rejects_bad_sequence_and_overlarge_windows():
    payload = _payload(_compiled(_program()))
    too_large = _node(payload, [dict(channel=0, start_s=0, duration_s=0.1)])
    assert too_large.returncode != 0
    assert "shorter window" in too_large.stderr
    payload["points"][0]["cards"][0]["steps"][0]["end"] = False
    payload["points"][0]["cards"][0]["steps"][0]["next_step"] = 0
    invalid = _node(payload, [dict(channel=0, start_s=0, duration_s=1e-6)])
    assert invalid.returncode != 0
    assert "next_step" in invalid.stderr


def test_public_compile_virtual_z_replayed_without_reapplying_frame():
    group = SpectrumGroup(
        "trigger",
        {"A": SpectrumCard("SIM_A", (0,))},
        {"rf": SpectrumOutput("A", 0)},
    )
    setup = DeviceSetup(
        {
            "trigger": Device("trigger", "ttl", grid_mu=1),
            "rf": Device("rf", "spectrum"),
        },
        spectrum=SpectrumConfig({"g": group}),
    )
    q = Qubit("q")
    exp = Experiment("Two phase frames", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay(q.rf, Square(400 * ns, amplitude=0.5, frequency=125e6))
        rz(q, math.pi / 2)
        trigplay(q.rf, Square(400 * ns, amplitude=0.5, frequency=125e6))
    compiled = LabConfig(setup, SignalMap({"q.rf": "rf"}), "artiq_spectrum").compile(
        exp
    )
    replay = simulate_output(compiled, start_s=400e-9, duration_s=400e-9)["traces"][0]
    target = np.rint(
        0.5 * 32767 * np.cos(-math.pi / 2 + 2 * np.pi * 0.1 * np.arange(500))
    )
    np.testing.assert_allclose(replay["dac_codes"], target, atol=1)
    assert replay["time_s"][0] == pytest.approx(400e-9, abs=1e-20)
    compiled.name = "mutation"
    with pytest.raises(CompileError, match="modified"):
        simulate_output(compiled)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None
        self.external = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attributes = dict(attrs)
            self.current = {"text": "", "type": attributes.get("type", "javascript")}
            self.scripts.append(self.current)
            if "src" in attributes:
                self.external.append(attributes["src"])

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.current = None


def test_self_contained_html_escapes_content_and_javascript_parses(tmp_path):
    compiled = _compiled(
        _program(), name="__SCRIPT__ test </script><script>alert('bad')</script>"
    )
    path = write_output_simulator(compiled, tmp_path / "waveform.html")
    assert path == tmp_path / "waveform.html"
    html = path.read_text(encoding="utf-8")
    assert write_output_simulator(compiled) == html
    parser = _Scripts()
    parser.feed(html)
    assert not parser.external
    assert len(parser.scripts) == 3
    assert "alert('bad')" not in parser.scripts[-1]["text"]
    assert "Ideal DAC reconstruction" in html
    assert "DecompressionStream" in html
    assert 'query.get("logical_id")' in html
    assert 'query.get("t0")' in html and 'query.get("t1")' in html
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    for i, script in enumerate(parser.scripts):
        if script["type"] == "application/json":
            json.loads(script["text"])
            continue
        source = tmp_path / f"script_{i}.js"
        source.write_text(script["text"], encoding="utf-8")
        process = subprocess.run(
            [node, "--check", str(source)], capture_output=True, text=True, timeout=30
        )
        assert process.returncode == 0, process.stderr
