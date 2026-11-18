"""DDS browser curves share execution offsets without changing AWG decoding."""

import base64
import copy
import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

JS = Path(__file__).parents[1] / "src/taiyidsl/reporting/assets/output_simulator.js"


def _dds():
    return {
        "group": "g",
        "region_id": "r0",
        "card": "C",
        "mode": "dds",
        "active_channels": [0],
        "trigger_latency_ns": 560,
        "command_start_s": 0,
        "command_end_s": 2e-6,
        "curves": [
            {
                "core": 0,
                "channel": 0,
                "time_s": [0, 1e-6, 1e-6, 1.25e-6, 2e-6, 4e-6],
                "frequency_hz": [70e6, 80e6, 79e6, 79e6, 70e6, 70e6],
                "amplitude": [0.08, 0.10, 0.095, 0.095, 0.08, 0.08],
                "frame": [0, 0, 1, 1, 2, 2],
                "stage": [
                    "frame_start",
                    "ramp_end_before_next_commit",
                    "frame_start",
                    "ramp_start",
                    "frame_start",
                    "terminal_hold",
                ],
            }
        ],
    }


def _payload():
    raw = struct.pack("<4h", 100, 200, -100, 0)
    digest = hashlib.sha256(raw).hexdigest()
    view = {
        "group": "g",
        "card": "A",
        "region_id": "r0",
        "sample0_time_s": 0,
        "sample0_fraction": ["0", "1"],
        "sample_rate_hz": 1_000_000,
        "physical_duration_samples": 4,
        "active_channels": [0],
        "output_range_mv": 500,
        "pulses": [],
        "segments": [{"id": 0, "sha256": digest, "sample_count": 4}],
        "steps": [
            {"index": 0, "segment_id": 0, "loops": 1, "next_step": 0, "end": True}
        ],
    }
    return {
        "name": "DDS UI fixture",
        "points": [{"index": 0, "coordinates": {}, "cards": [view], "dds": [_dds()]}],
        "packed": {
            digest: {
                "byte_length": len(raw),
                "encoding": "zlib-base64",
                "data": base64.b64encode(zlib.compress(raw)).decode(),
            }
        },
    }


def _node(script, payload):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for browser command-curve checks")
    process = subprocess.run(
        [node, "-e", script, str(JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_dds_clipping_preserves_pins_ramp_delay_and_nonzero_hold():
    result = _node(
        """
const fs = require('node:fs'), api = require(process.argv[1]);
const view = JSON.parse(fs.readFileSync(0,'utf8'));
const clipped = api.ddsWindow(view,{start_s:.5e-6,duration_s:2e-6});
const pinned = api.ddsWindow(view,{start_s:1e-6,duration_s:.25e-6});
const outside = api.ddsWindow(view,{start_s:5e-6,duration_s:1e-6});
process.stdout.write(JSON.stringify({clipped,pinned,outside}));
""",
        _dds(),
    )
    curve = result["clipped"]["curves"][0]
    assert curve["time_s"] == pytest.approx([0.5e-6, 1e-6, 1e-6, 1.25e-6, 2e-6, 2.5e-6])
    assert curve["frequency_hz"] == pytest.approx([75e6, 80e6, 79e6, 79e6, 70e6, 70e6])
    assert curve["amplitude"] == pytest.approx([0.09, 0.10, 0.095, 0.095, 0.08, 0.08])
    assert result["pinned"]["curves"][0]["frequency_hz"] == [80e6, 79e6, 79e6]
    assert result["outside"] == {"curves": [], "vertices": 0}


def test_mixed_replay_preserves_awg_codes_and_clips_hold_at_execution_boundaries():
    result = _node(
        """
const fs = require('node:fs'), assert = require('node:assert/strict');
const api = require(process.argv[1]), payload = JSON.parse(fs.readFileSync(0,'utf8'));
const entries = [
 {point:0,shot:0,ordinal:0,offset_s:0,start_s:0,end_s:3e-6},
 {point:0,shot:1,ordinal:1,offset_s:5e-6,start_s:5e-6,end_s:8e-6}];
const window = {start_s:0,duration_s:8e-6};
(async()=>{
 const mixed = await api.replayExecutions(payload,entries,window,{units:'codes'});
 const volts = await api.replayExecutions(payload,entries,window,{units:'voltage'});
 const awg = structuredClone(payload); awg.points[0].dds=[];
 const baseline = await api.replayExecutions(awg,entries,window,{units:'codes'});
 assert.deepEqual(mixed.traces[0],baseline.traces[0]);
 assert.deepEqual(mixed.traces.slice(1),volts.traces.slice(1));
 assert.equal(mixed.requested,baseline.requested);
 assert(mixed.traces.slice(1).every(t=>t.x.every(x=>x===null || x<=3 || x>=5)));
 assert.deepEqual(mixed.traces[0].y.filter(x=>x!==null),[100,200,-100,0,100,200,-100]);
 process.stdout.write(JSON.stringify({axes:api.outputAxes('codes',true,true),
   traces:mixed.traces.map(t=>({axis:t.yaxis||'y',uid:t.uid})),vertices:mixed.ddsVertices}));
})().catch(e=>{console.error(e.stack);process.exit(1)});
""",
        _payload(),
    )
    assert [trace["axis"] for trace in result["traces"]] == ["y", "y2", "y3"]
    assert result["vertices"] == 12
    assert result["axes"]["yaxis3"]["domain"][1] < result["axes"]["yaxis2"]["domain"][0]
    assert result["axes"]["yaxis2"]["domain"][1] < result["axes"]["yaxis"]["domain"][0]


def test_dds_only_ui_selects_tones_and_keeps_shared_zoom_and_visibility():
    payload = copy.deepcopy(_payload())
    payload["points"][0]["cards"] = []
    window = {"start_fraction": ["0", "1"], "end_fraction": ["3", "1000000"]}
    payload["execution_axis"] = {
        "shots": 2,
        "scope": "experiment",
        "mode": "cyclic",
        "total_executions": 2,
        "points": [
            {"index": 0, "layout": {"chunk": 0}, "awg": window, "control": window}
        ],
    }
    result = _node(
        r"""
const fs=require('node:fs'), assert=require('node:assert/strict');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
const selects=new Set(['point','group','card','channel','pulse','units',
 'display','selection-mode','projection']);
class Element {
 constructor(tag) {this.tag=tag;this.value='';this.children=[];
  this.listeners={};this.style={};this.textContent='';}
 append(...nodes) {for(const node of nodes) {this.children.push(node);
   if(this.tag==='select' && this.children.length===1)this.value=String(node.value);}}
 replaceChildren() {this.children=[];if(this.tag==='select')this.value='';}
 addEventListener(name,fn) {(this.listeners[name] ||= []).push(fn);}
 on(name,fn) {this.addEventListener(name,fn);}
 emit(name,event={}) {for(const fn of this.listeners[name]||[])fn(event);}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))
 elements.set(id,new Element(selects.has(id)?'select':'div'));
 return elements.get(id);};
get('output-data').textContent=JSON.stringify(payload);
get('units').value='codes';get('display').value='lines';
global.document={getElementById:get,createElement:tag=>new Element(tag),
 createDocumentFragment:()=>new Element('fragment')};
global.location={search:''};
let traces,layout,plots=0;
global.Plotly={react:async(id,data,next)=>{traces=data;layout=next;plots++;},
 restyle:(id,patch,indices)=>indices.forEach(i=>Object.assign(traces[i],patch))};
require(require('node:path').join(require('node:path').dirname(process.argv[1]),'viewer_execution.js'));
require(process.argv[1]);
async function plotted(before){for(let i=0;i<1000&&plots<=before;i++)
 await new Promise(r=>setTimeout(r,1));
 assert(plots>before,get('notice').textContent);}
(async()=>{
 await plotted(0);
 assert.equal(traces.length,2);assert.equal(get('card').value,'C');
 assert.match(get('metadata').textContent,/DDS command prediction/);
 assert.equal(get('focus-pulse').disabled,true);
 assert.equal(layout.yaxis.visible,false);assert.equal(layout.xaxis.anchor,'free');
 assert(traces.every(t=>!t.xaxis));
 assert.match(get('notice').textContent,/not RF\/DAC samples/);
 get('waveform').emit('plotly_legendclick',{curveNumber:1});
 let before=plots;get('waveform').emit('plotly_relayout',{
  'xaxis.range[0]':.5,'xaxis.range[1]':1.5});
 await plotted(before);
 assert.deepEqual(layout.xaxis.range,[.5,1.5]);assert.equal(traces[1].visible,'legendonly');
 const amplitudes=traces[1].y;
 before=plots;get('units').value='voltage';get('units').emit('change');
 await plotted(before);
 assert.deepEqual(traces[1].y,amplitudes);assert.equal(traces[1].visible,'legendonly');
 assert.match(get('sample-note').textContent,/No finite-program samples/);
 process.stdout.write(JSON.stringify({traces:traces.length,plots,card:get('card').value}));
})().catch(e=>{console.error(e.stack);process.exit(1)});
""",
        payload,
    )
    assert result == {"traces": 2, "plots": 3, "card": "C"}
