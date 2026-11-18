"""CSS-safe Plotly identities and gate/frame inspection without browser UI."""

import copy
import math

import pytest
from test_dds_viewer_js import _dds
from test_waveform_pulse_coloring import _fixture, _node


def _annotation(name, index, *, recovery=False):
    return {
        "native_gate": name,
        "clifford_index": index,
        "clifford_position": 4 if recovery else 1,
        "recovery": recovery,
        "implementation_step": 0,
    }


def _annotated_payload():
    _, payload = _fixture()
    point = payload["points"][0]
    pulses = point["cards"][0]["pulses"]
    for pulse, label in zip(
        pulses,
        (
            _annotation("x90", 15),
            _annotation('x<90>&"', 13),
            _annotation("xm90", 19, recovery=True),
        ),
        strict=True,
    ):
        pulse["gate_annotations"] = [label]
    frame = 0
    point["virtual_z"] = []
    for position, (time, angle, name, clifford, recovery) in enumerate(
        (
            (0, math.pi, "z180", 19, False),
            (0, math.pi / 2, "z90", 15, False),
            (61.6e-9, math.pi / 2, "z90", 15, False),
            (272e-9, math.pi, "z180", 19, True),
        )
    ):
        point["virtual_z"].append(
            {
                "time_s": time,
                "logical_ids": [f"z{position}"],
                "signal": "q0",
                "group": "qubit_awg",
                "region_id": "r0",
                "angle_rad": angle,
                "frame_before_rad": frame,
                "frame_after_rad": frame - angle,
                "gate_annotations": [_annotation(name, clifford, recovery=recovery)],
                "channels": [{"group": "qubit_awg", "card": "A", "channel": 0}],
            }
        )
        frame -= angle
    return payload


def test_all_trace_uids_are_css_safe_stable_and_unique_for_awg_dds_and_pulses():
    _, payload = _fixture()
    payload["points"][0]["cards"][0]["group"] = 'g["引用"]\\beam'
    payload["points"][0]["dds"] = [_dds()]
    result = _node(
        payload,
        r"""
const fs=require('node:fs'),assert=require('node:assert/strict');
const api=require(process.argv[1]),payload=JSON.parse(fs.readFileSync(0,'utf8'));
const entries=[{point:0,shot:0,ordinal:0,offset_s:0},
  {point:0,shot:1,ordinal:1,offset_s:0.00001}];
(async()=>{
 const full=await api.replayExecutions(payload,entries,
   {start_s:-0.000000112,duration_s:0.00002});
 const zoom=await api.replayExecutions(payload,entries,
   {start_s:0.00000001,duration_s:0.0000001});
 const channels=await api.replayExecutions(payload,entries,
   {start_s:-0.000000112,duration_s:0.00002},{color_by:'channel'});
 const semantic=t=>JSON.stringify([t.legendgroup,t.meta||null,t.yaxis||'y']);
 const ids=new Map(full.traces.map(t=>[semantic(t),t.uid]));
 for(const render of [full,zoom,channels]) {
   assert.equal(new Set(render.traces.map(t=>t.uid)).size,render.traces.length);
   for(const trace of render.traces) {
     assert.match(trace.uid,/^[A-Za-z][A-Za-z0-9_-]*$/,
       'Plotly .cb'+trace.uid+' must be a valid CSS class selector');
   }
 }
 for(const trace of zoom.traces) assert.equal(trace.uid,ids.get(semantic(trace)));
 assert(full.traces.some(t=>t.meta?.kind==='pulse'));
 assert(full.traces.some(t=>t.yaxis==='y2'));
 assert(full.traces.some(t=>t.yaxis==='y3'));
 assert.notEqual(api.traceUid('a:b'),api.traceUid('a_b'));
 assert.notEqual(api.traceUid('a','b'),api.traceUid('a,b'));
 process.stdout.write(JSON.stringify({passed:true}));
})().catch(error=>{console.error(error.stack);process.exit(1);});
""",
    )
    assert result == {"passed": True}


def test_gate_hover_and_zero_time_virtual_z_keep_order_recovery_and_shot_offsets():
    payload = _annotated_payload()
    generic = copy.deepcopy(payload)
    generic["points"][0]["cards"] = []
    generic["packed"] = {}
    for marker in generic["points"][0]["virtual_z"]:
        marker["gate_annotations"] = []
    result = _node(
        {"payload": payload, "generic": generic},
        r"""
const fs=require('node:fs'),assert=require('node:assert/strict');
const api=require(process.argv[1]),input=JSON.parse(fs.readFileSync(0,'utf8'));
const entries=[{point:0,shot:0,ordinal:0,offset_s:0},
  {point:0,shot:1,ordinal:1,offset_s:0.00001}];
const window={start_s:-0.000000112,duration_s:0.000011};
(async()=>{
 const full=await api.replayExecutions(input.payload,entries,window,{units:'codes'});
 const plain=await api.replayExecutions(input.generic,entries,window,{units:'codes'});
 process.stdout.write(JSON.stringify({full,plain,axes:api.outputAxes('codes',false,true)}));
})().catch(error=>{console.error(error.stack);process.exit(1);});
""",
    )
    full = result["full"]
    pulse_traces = [
        t for t in full["traces"] if t.get("meta", {}).get("kind") == "pulse"
    ]
    descriptions = {
        t["meta"]["pulse_indices"][0]: "\n".join(
            row[3] for row in t["customdata"] if row
        )
        for t in pulse_traces
    }
    assert "x90" in descriptions[0] and "Clifford #15" in descriptions[0]
    assert "x&lt;90&gt;&amp;" in descriptions[1]
    assert "x<90>" not in descriptions[1]
    assert "Clifford #19" in descriptions[2]
    assert "recovery" in descriptions[2].lower()
    assert full["virtualZCount"] == 8
    marker_traces = [
        trace
        for trace in full["traces"]
        if trace.get("meta", {}).get("kind") == "virtual_z"
    ]
    assert marker_traces
    markers = {}
    for trace in marker_traces:
        assert trace["yaxis"] == "y4"
        assert not trace["showlegend"]
        for x, row in zip(trace["x"], trace["customdata"], strict=True):
            if row is not None:
                markers.setdefault((row[0], x), set()).add(row[3])
    # Coincident operations share a line; no artificial time offsets are added.
    assert sorted(x for ordinal, x in markers if ordinal == 0) == pytest.approx(
        [0, 0.0616, 0.272]
    )
    assert sorted(x for ordinal, x in markers if ordinal == 1) == pytest.approx(
        [10, 10.0616, 10.272]
    )
    coincident = "\n".join(markers[0, 0])
    assert coincident.index("z180") < coincident.index("z90")
    assert "Clifford #19" in coincident and "Clifford #15" in coincident
    terminal = next(
        v for (ordinal, x), v in markers.items() if ordinal == 0 and x > 0.2
    )
    assert "recovery" in "\n".join(terminal).lower()
    for (_, time), details in list(markers.items()):
        if time < 1:
            matching = next(
                value
                for (ordinal, x), value in markers.items()
                if ordinal == 1 and x == pytest.approx(time + 10)
            )
            assert matching == details
    assert result["axes"]["yaxis4"]["overlaying"] == "y"
    assert result["axes"]["yaxis4"]["range"] == [0, 1]
    plain = result["plain"]
    assert plain["virtualZCount"] == 8
    assert plain["requested"] == plain["exact"] == 0
    assert plain["firstTable"] is None
    for trace in plain["traces"]:
        if trace.get("meta", {}).get("kind") == "virtual_z":
            assert not any("Clifford" in row[3] for row in trace["customdata"] if row)
        else:
            assert all(value is None for value in trace["y"])


def test_zoom_cleanup_uses_valid_selectors_through_modes_and_shots():
    """Model Plotly selector cleanup, not a rendered-browser acceptance test."""
    payload = _annotated_payload()
    payload["execution_axis"].update(shots=2, total_executions=2)
    result = _node(
        payload,
        r"""
const fs=require('node:fs'),assert=require('node:assert/strict');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
const selects=new Set(['point','group','card','channel','pulse','units','display',
  'selection-mode','projection','color-by']);
class Element {
 constructor(tag) {this.tag=tag;this.value='';this.children=[];this.listeners={};
   this.textContent='';this.className='';}
 append(...nodes) {for(const node of nodes) {
   if(node.tag==='fragment') {this.append(...node.children);continue;}
   this.children.push(node);
   if(this.tag==='select' && this.children.length===1)this.value=String(node.value);
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
let traces=[],plots=0,cleaned=0;
global.Plotly={react:async(id,data)=>{
 // The old JSON uid made this exact class-selector construction invalid.
 for(const old of traces) {
   const selector='.cb'+old.uid;
   if(!/^\.[A-Za-z][A-Za-z0-9_-]*$/.test(selector))
     throw new SyntaxError(selector+' is not a valid selector');
   cleaned++;
 }
 assert.equal(new Set(data.map(t=>t.uid)).size,data.length);
 traces=data;plots++;
},restyle:(id,patch,indices)=>indices.forEach(i=>Object.assign(traces[i],patch))};
require(require('node:path').join(require('node:path').dirname(process.argv[1]),
 'viewer_execution.js'));
require(process.argv[1]);
async function plotted(before) {
 for(let i=0;i<1000 && plots<=before;i++)
   await new Promise(resolve=>setTimeout(resolve,1));
 assert(plots>before,get('notice').textContent);
 assert.notEqual(get('notice').className,'error');
}
(async()=>{
 await plotted(0);
 let before=plots;
 get('waveform').emit('plotly_relayout',{'xaxis.range':[0.12,0.2]});
 await plotted(before);
 for(const mode of ['channel','pulse']) {
   before=plots;get('color-by').value=mode;get('color-by').emit('change');
   await plotted(before);
 }
 before=plots;get('selection-mode').value='detail';
 get('selection-mode').emit('change');await plotted(before);
 const firstIds=traces.map(t=>t.uid).sort();
 before=plots;get('shot-index').value='1';get('shot-index').emit('change');
 await plotted(before);
 assert.deepEqual(traces.map(t=>t.uid).sort(),firstIds);
 before=plots;
 get('waveform').emit('plotly_relayout',{'xaxis.range':[0,0.1]});
 await plotted(before);
 assert(cleaned>0);
 process.stdout.write(JSON.stringify({plots,cleaned}));
})().catch(error=>{console.error(error.stack);process.exit(1);});
""",
    )
    assert result["plots"] == 7
    assert result["cleaned"] > 0
