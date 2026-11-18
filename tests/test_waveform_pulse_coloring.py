"""Pulse-colored display preserves final DAC samples and physical boundaries."""

import copy
import json
import math
import shutil
import subprocess
from fractions import Fraction

import pytest
from test_output_simulator import JS_PATH, _compiled, _program, _pulse

from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.reporting.output_simulator import _payload


def _fixture():
    # Non-round boundaries, adjacent pulses, idle, and a pulse crossing tiles.
    pulses = [
        _pulse(start_sample=140, duration_samples=77, phase_rad=0),
        _pulse(start_sample=217, duration_samples=81, phase_rad=math.pi / 2),
        _pulse(start_sample=350, duration_samples=130, phase_rad=3 * math.pi / 2),
    ]
    program = _program(pulses, duration=700, max_tile_samples=384)
    return program, _payload(_compiled(program))


def _node(payload, script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for waveform display checks")
    process = subprocess.run(
        [node, "-e", script, str(JS_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


REPLAY = r"""
const fs = require('node:fs');
const api = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const results = [];
  for (const request of input.requests) {
    const result = await api.replayExecutions(input.payload, request.entries,
      request.window, {units:'codes', ...request.options});
    results.push({overview:result.overview, exact:result.exact, traces:result.traces});
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => {console.error(error.stack);process.exit(1);});
"""


def _samples(result):
    for trace in result["traces"]:
        for x, y, row in zip(trace["x"], trace["y"], trace["customdata"], strict=True):
            if row is not None and x is not None:
                yield trace, x, y, row


def test_exact_pulse_colors_survive_zoom_regions_shots_and_channel_mode():
    program, payload = _fixture()
    first_view = payload["points"][0]["cards"][0]
    second_view = copy.deepcopy(first_view)
    second_view["region_id"] = "r1"
    origin = Fraction(*map(int, first_view["sample0_fraction"])) + Fraction(2, 10**6)
    second_view.update(
        sample0_fraction=[str(origin.numerator), str(origin.denominator)],
        sample0_time_s=float(origin),
        sample0_time_ns=float(origin * 10**9),
    )
    payload["points"][0]["cards"].append(second_view)
    entries = [
        {"ordinal": 0, "point": 0, "shot": 0, "offset_s": 0},
        {"ordinal": 1, "point": 0, "shot": 1, "offset_s": 0.00001},
    ]
    full = {"start_s": "-0.000000112", "duration_s": "0.000013"}
    zoom = {
        "start_s": str(float(origin + Fraction(160, 1250000000))),
        "duration_s": str(float(Fraction(100, 1250000000))),
    }
    requests = [
        {"entries": entries, "window": full, "options": {}},
        {"entries": entries[:1], "window": zoom, "options": {}},
        {"entries": entries, "window": full, "options": {"color_by": "channel"}},
        {"entries": entries, "window": full, "options": {"display": "markers"}},
    ]
    colored, cropped, channels, markers = _node(
        {"payload": payload, "requests": requests}, REPLAY
    )
    expected = reconstruct_window(program, 0, program["physical_duration_samples"])
    colors = {}
    actual_samples = []
    for trace, displayed_us, value, row in _samples(colored):
        channel = json.loads(trace["legendgroup"])[2]
        assert value == row[5] == int(expected[row[4], channel])
        owner = row[6]
        if owner["boundary"]:
            continue
        actual_samples.append((row[0], row[4], channel, row[5]))
        pulse_index = next(
            (
                i
                for i, (a, b) in enumerate(((140, 217), (217, 298), (350, 480)))
                if channel == 0 and a <= row[4] < b
            ),
            None,
        )
        if pulse_index is None:
            assert owner["kind"] == "idle"
            assert owner["pulse_indices"] == []
        else:
            local_us = displayed_us - (10 if row[0] == 1 else 0)
            region = 0 if local_us < 1 else 1
            assert owner["kind"] == "pulse"
            assert owner["pulse_indices"] == [pulse_index + 3 * region]
        if owner["kind"] == "pulse":
            ordinal = owner["pulse_indices"][0]
            colors.setdefault(ordinal, trace["line"]["color"])
            assert trace["line"]["color"] == colors[ordinal]
            assert trace["meta"]["pulse_indices"] == [ordinal]
    assert len(actual_samples) == len(expected) * 2 * 2 * 2
    assert len(colors) == len(set(colors.values())) == 6
    idle_colors = {
        t["line"]["color"] for t in colored["traces"] if t["meta"]["kind"] == "idle"
    }
    assert len(idle_colors) == 1
    assert not idle_colors.intersection(colors.values())
    assert len({t["uid"] for t in colored["traces"]}) == len(colored["traces"])
    for group in {t["legendgroup"] for t in colored["traces"]}:
        assert (
            sum(t["showlegend"] for t in colored["traces"] if t["legendgroup"] == group)
            == 1
        )
    for trace, _, value, row in _samples(cropped):
        channel = json.loads(trace["legendgroup"])[2]
        assert value == int(expected[row[4], channel])
        if row[6]["kind"] == "pulse" and not row[6]["boundary"]:
            assert trace["line"]["color"] == colors[row[6]["pulse_indices"][0]]
    channel_samples = [
        (r[0], r[4], json.loads(t["legendgroup"])[2], r[5])
        for t, _, _, r in _samples(channels)
        if not r[6]["boundary"]
    ]
    assert sorted(channel_samples) == sorted(actual_samples)
    assert len(channels["traces"]) == 2
    assert all(t["meta"]["kind"] == "channel" for t in channels["traces"])
    assert not any(r[6]["boundary"] for _, _, _, r in _samples(markers))


def test_overlapping_display_annotations_do_not_replace_final_dac_values():
    # The compiler forbids overlapping same-channel pulses. Exercise defensive
    # display classification using inconsistent annotations, not a new waveform.
    program, payload = _fixture()
    payload["points"][0]["cards"][0]["pulses"].append(
        _pulse(start_sample=160, duration_samples=100)
    )
    request = {
        "entries": [{"ordinal": 0, "point": 0, "shot": 0, "offset_s": 0}],
        "window": {"start_s": "-0.000000112", "duration_s": "0.000001"},
        "options": {},
    }
    (result,) = _node({"payload": payload, "requests": [request]}, REPLAY)
    expected = reconstruct_window(program, 0, program["physical_duration_samples"])
    overlap_samples = []
    for trace, _, value, row in _samples(result):
        channel = json.loads(trace["legendgroup"])[2]
        assert value == int(expected[row[4], channel])
        if row[6]["kind"] == "overlap" and not row[6]["boundary"]:
            assert len(row[6]["pulse_indices"]) == 2
            assert trace["meta"]["kind"] == "overlap"
            overlap_samples.append(row[4])
    assert sorted(overlap_samples) == list(range(160, 260))


def test_overview_bins_stop_at_pulse_boundaries_and_keep_exact_extrema():
    program, payload = _fixture()
    request = {
        "entries": [{"ordinal": 0, "point": 0, "shot": 0, "offset_s": 0}],
        # A wide viewport allocates one ordinary bin to this short program;
        # pulse boundaries must split that bin before extrema are calculated.
        "window": {"start_s": "-0.000000112", "duration_s": "0.01"},
        "options": {"max_samples": 10},
    }
    (result,) = _node({"payload": payload, "requests": [request]}, REPLAY)
    assert result["overview"]
    expected = reconstruct_window(program, 0, program["physical_duration_samples"])
    boundaries = [140, 217, 298, 350, 480]
    covered = {0: [], 1: []}
    for trace in result["traces"]:
        channel = json.loads(trace["legendgroup"])[2]
        rows = list(_samples({"traces": [trace]}))
        # Each min/max rectangle has four vertices followed by a null break.
        assert len(rows) % 4 == 0
        for index in range(0, len(rows), 4):
            rectangle = rows[index : index + 4]
            row = rectangle[0][3]
            start, stop = row[4], row[7]
            if channel == 0:
                assert not any(start < boundary < stop for boundary in boundaries)
            chunk = expected[start:stop, channel]
            assert [vertex[2] for vertex in rectangle] == [
                int(chunk.min()),
                int(chunk.max()),
                int(chunk.max()),
                int(chunk.min()),
            ]
            assert all(vertex[3][6] == row[6] for vertex in rectangle)
            assert not row[6]["boundary"]
            covered[channel].append((start, stop))
    for spans in covered.values():
        spans.sort()
        assert spans[0][0] == 0
        assert spans[-1][1] == len(expected)
        assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))


def test_dom_color_switch_and_legend_hide_whole_channel_across_zoom():
    """Exercise event handlers with a DOM mock, not rendered-browser QA."""
    _, payload = _fixture()
    script = r"""
const fs=require('node:fs'), assert=require('node:assert/strict');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
const selects=new Set(['point','group','card','channel','pulse','units','display',
  'selection-mode','projection','color-by']);
class Element {
  constructor(tag) {this.tag=tag;this.value='';this.children=[];this.listeners={};
    this.textContent='';this.className='';}
  append(...nodes) {for(const node of nodes) {
    if(node.tag==='fragment') {this.append(...node.children);continue;}
    this.children.push(node);
    if(this.tag==='select' && this.children.length===1) this.value=String(node.value);
  }}
  replaceChildren() {this.children=[];if(this.tag==='select')this.value='';}
  addEventListener(name,fn) {(this.listeners[name] ||= []).push(fn);}
  on(name,fn) {this.addEventListener(name,fn);}
  emit(name,event={}) {for(const fn of this.listeners[name] || [])fn(event);}
}
const elements=new Map();
function get(id) {
  if(!elements.has(id))elements.set(id,new Element(selects.has(id)?'select':'div'));
  return elements.get(id);
}
get('output-data').textContent=JSON.stringify(payload);
get('units').value='codes';get('display').value='lines';
global.document={getElementById:get,createElement:tag=>new Element(tag),
  createDocumentFragment:()=>new Element('fragment')};
global.location={search:''};
let traces,plots=0;
global.Plotly={react:async(id,data)=>{traces=data;plots++;},
  restyle:(id,patch,indices)=>indices.forEach(i=>Object.assign(traces[i],patch))};
require(require('node:path').join(require('node:path').dirname(process.argv[1]),
  'viewer_execution.js'));
require(process.argv[1]);
async function plotted(previous) {
  for(let i=0;i<1000 && plots<=previous;i++)
    await new Promise(resolve=>setTimeout(resolve,1));
  assert(plots>previous,get('notice').textContent);
}
const group=JSON.stringify(['qubit_awg','A',0]);
const channel=()=>traces.filter(t=>t.legendgroup===group);
(async()=>{
  await plotted(0);
  assert.equal(get('color-by').value,'pulse');
  const beforeColors=channel().filter(t=>t.meta.kind==='pulse').map(t=>t.line.color);
  assert.equal(new Set(beforeColors).size,3);
  const legend=traces.findIndex(t=>t.legendgroup===group && t.showlegend);
  get('waveform').emit('plotly_legendclick',{curveNumber:legend});
  assert(channel().every(t=>t.visible==='legendonly'));
  assert(traces.filter(t=>t.legendgroup!==group).every(t=>t.visible===true));
  let before=plots;
  get('color-by').value='channel';get('color-by').emit('change');await plotted(before);
  assert.equal(traces.length,2);
  assert(channel().every(t=>t.visible==='legendonly'));
  before=plots;get('color-by').value='pulse';get('color-by').emit('change');
  await plotted(before);
  assert.deepEqual(channel().filter(t=>t.meta.kind==='pulse').map(t=>t.line.color),
    beforeColors);
  assert(channel().every(t=>t.visible==='legendonly'));
  before=plots;
  get('waveform').emit('plotly_relayout',{'xaxis.range[0]':0.04,'xaxis.range[1]':0.12});
  await plotted(before);
  assert(channel().every(t=>t.visible==='legendonly'));
  const shown=traces.findIndex(t=>t.legendgroup===group && t.showlegend);
  get('waveform').emit('plotly_legendclick',{curveNumber:shown});
  assert(channel().every(t=>t.visible===true));
  process.stdout.write(JSON.stringify({passed:true}));
})().catch(error=>{console.error(error.stack);process.exit(1);});
"""
    assert _node(payload, script) == {"passed": True}
