"""Pulse-sheet blocks preserve physical timing and expose safe inspection data."""

import json
import shutil
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_spectrum_backend import lab

from taiyidsl import Experiment, Qubit, Square, play, reset_phase, rz, trigplay, us
from taiyidsl.viewer_execution import execution_axis
from taiyidsl.visualization import _APPLICATION_JS, _payload, _trace, visualize


def test_hybrid_physical_sheet_expands_card_outputs_on_correct_timebase():
    profile = lab(cooling=True)
    config = profile.setup.spectrum
    group = config.groups["g"]
    profile.setup.spectrum = replace(
        config,
        groups={
            "g": replace(
                group,
                cards={
                    **group.cards,
                    "B": replace(group.cards["B"], trigger_latency_samples=501),
                },
            )
        },
    )
    q = Qubit("q")
    exp = Experiment("sheet_clock_units", [q])
    with exp.average(1), exp.realtime():
        play("cooling", Square(20 * us, frequency=80e6))
        with exp.triggered_sequence(group="g"):
            with exp.parallel():
                trigplay(q.rf, Square(4 * us, frequency=30_000))
                trigplay("clock", Square(4 * us, frequency=100e6))
            rz(q, 0.4)
            trigplay(q.rf, Square(4 * us, frequency=30_000, phase=0.2))
    compiled = profile.compile(exp)
    point = compiled.points[0]
    data = _payload(compiled)[0]
    outputs = [
        trace for trace in data["physical"] if trace["name"] == "spectrum_output"
    ]
    assert len(outputs) == 3
    origin_us = point.origin_mu * 0.2e-3
    for trace, bounds in zip(outputs, [(20, 24), (24, 28), (20, 24)]):
        assert trace["x"] == pytest.approx([origin_us + value for value in bounds])
    assert {trace["meta"]["lane"] for trace in outputs} == {
        "AWG A · ch0",
        "AWG B · ch0",
    }
    assert all(trace["meta"]["role"] == "output" for trace in outputs)
    assert outputs[0]["meta"]["waveform"]["card"] == "A"
    assert outputs[0]["meta"]["waveform"]["group"] == "g"
    assert outputs[0]["meta"]["waveform"]["logical_id"]
    assert outputs[0]["meta"]["waveform"]["t0"] == "0.00002"
    assert outputs[0]["meta"]["waveform"]["t1"] == "0.000024"
    assert outputs[1]["meta"]["details"]["parameters"]["virtual_z_phase"] == 0.4
    assert "virtual_z_phase" not in outputs[1]["hovertemplate"]
    program_markers = [t for t in data["physical"] if t["name"] == "spectrum_program"]
    assert len(program_markers) == 1
    assert program_markers[0]["x"][0] == program_markers[0]["x"][1]
    assert any(t["meta"]["lane"] == "AWG B · ch1" for t in data["physical"])
    cooling = next(t for t in data["physical"] if t["name"] == "dds_output")
    assert cooling["x"] == pytest.approx([origin_us, origin_us + 20])
    config = next(t for t in data["physical"] if t["name"] == "dds_config")
    assert config["meta"]["details"]["driver_parameter_time_unit_s"] == 1e-9
    assert config["meta"]["details"]["time_unit_s"] == 0.2e-9

    # Native fixed stages occupy known time; the variable prefix has no box.
    from examples.artiq_spectrum.simple_ramsey import build as native_build

    native_lab, native_exp = native_build()
    native_point = _payload(native_lab.compile(native_exp))[0]
    expected = [
        *native_point["native_lifecycle"]["timed"],
        *native_point["native_lifecycle"]["suffix"],
    ]
    for view in ("logical", "physical"):
        blocks = [
            t
            for t in native_point[view]
            if t["meta"]["lane"] == "Native ARTIQ · declared timing"
        ]
        assert len(blocks) == len(expected) == 2
        for block, call in zip(blocks, expected):
            shift = native_point["origin_us"] if view == "physical" else 0
            assert block["x"] == pytest.approx(
                [
                    call["start_s_from_control_T0"] * 1e6 + shift,
                    call["end_s_from_control_T0"] * 1e6 + shift,
                ]
            )
            assert block["meta"]["role"] == "structure"
            assert (
                block["meta"]["details"]["parameters"]["native_output"]
                == "opaque_not_simulated"
            )


def test_logical_frame_marker_and_wait_are_distinct_from_output():
    from taiyidsl import delay

    q = Qubit("q")
    exp = Experiment("sheet_frame", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay(q.rf, Square(4 * us, frequency=30_000))
        delay(2 * us)
        rz(q, 0.5)
    data = _payload(lab().compile(exp))[0]
    frame = next(t for t in data["logical"] if t["name"] == "rz")
    wait = next(t for t in data["logical"] if t["name"] == "scope_delay")
    gate = next(t for t in data["logical"] if t["name"] == "play")
    assert frame["x"] == [6.0, 6.0]
    assert frame["meta"]["role"] == "frame"
    assert wait["meta"]["role"] == "wait"
    assert gate["meta"]["engine"] == "Spectrum AWG"
    assert gate["meta"]["details"]["source"]


def test_reset_phase_is_a_frame_marker_and_details_identify_one_frame():
    q = Qubit("q")
    exp = Experiment("reset marker", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        rz(q, 0.4)
        reset_phase(q.frame)
        trigplay(q.rf, Square(4 * us, frequency=30_000))
    data = _payload(lab().compile(exp))[0]
    reset = next(trace for trace in data["logical"] if trace["name"] == "reset_phase")
    assert reset["meta"]["role"] == "frame"
    assert reset["meta"]["details"]["parameters"]["phase"] == 0
    output = next(
        trace for trace in data["physical"] if trace["name"] == "spectrum_output"
    )
    params = output["meta"]["details"]["parameters"]
    assert params["frame_phase"] == 0
    assert params["phase_evolution"]["carrier_reference_kind"] == "explicit_reset_phase"
    assert "Frame F (rad)" in _APPLICATION_JS
    assert "Hardware frame H" not in _APPLICATION_JS


@pytest.mark.parametrize("preparation_us", [19.6, 39.2, 64.4, 68.4])
def test_macro_links_preserve_exact_sample_boundary_decimal(preparation_us):
    from fractions import Fraction

    from taiyidsl import ns

    q = Qubit("q")
    exp = Experiment("exact_pulse_link", [q])
    with exp.average(1), exp.realtime():
        play("camera", Square(preparation_us * us))
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(400 * ns, frequency=30_000))
    point = _payload(lab().compile(exp))[0]
    output = next(t for t in point["physical"] if t["name"] == "spectrum_output")
    link = output["meta"]["waveform"]
    expected = Fraction(str(preparation_us)) / 10**6
    assert Fraction(link["t0"]) == expected
    assert Fraction(link["t1"]) == expected + Fraction(400, 10**9)
    logical = next(t for t in point["logical"] if t["meta"]["waveform"])
    assert logical["meta"]["waveform"] == link


def test_waveform_link_is_explicit_local_and_does_not_change_default_api(tmp_path):
    from test_artiq import make_compiled

    compiled = make_compiled()
    default = visualize(compiled, tmp_path / "plain.html").read_text(encoding="utf-8")
    linked = visualize(
        compiled, tmp_path / "linked.html", waveform_path="waveform view.html"
    ).read_text(encoding="utf-8")
    assert '"cyclic", null' in default
    assert '"cyclic", "waveform%20view.html"' in linked
    assert all(
        trace["meta"]["waveform"] is None for trace in _payload(compiled)[0]["physical"]
    )
    with pytest.raises(ValueError, match="relative local"):
        visualize(compiled, tmp_path / "bad.html", waveform_path="javascript:alert(1)")


def test_hover_keeps_quotes_readable_and_nested_metadata_inert():
    from test_artiq import EventFixture

    event = EventFixture(
        "play",
        'rf "primary" <img src=x onerror="boom">',
        0,
        20,
        {
            "frequency": 30_000,
            "envelope": {"kind": "square", "note": 'a "quoted" label'},
            "nested": {"source": "</script><script>boom</script>"},
            "template": "%{x}",
        },
        source='file.py:20 "test" <b>literal</b>',
    )
    trace = _trace(event, 1e-9)
    hover = trace["hovertemplate"]
    assert "&quot;" not in hover and "&#x27;" not in hover
    assert "30000" in hover
    assert "<img" not in hover and "<script>" not in hover
    assert "&lt;img" in hover
    assert "Parameters" not in hover and "Source" not in hover
    assert "envelope" not in hover and "nested" not in hover
    assert "%{x}" not in hover
    # Complete potentially long content remains available only on selection.
    assert trace["meta"]["details"]["parameters"]["nested"] == event.params["nested"]
    assert trace["meta"]["details"]["source"] == event.source


def test_all_shots_is_default_and_single_index_remains_available(tmp_path):
    from test_artiq import make_compiled

    compiled = make_compiled()
    compiled.shots = 123
    html = visualize(compiled, tmp_path / "all-shots.html").read_text(encoding="utf-8")
    assert '<select id="shot">' in html
    assert '<option value="all" selected>All shots</option>' in html
    assert '<option value="all" selected>All points</option>' in html
    assert '<option value="single">One shot index</option>' in html
    assert '<details class="advanced">' in html
    assert '<details class="advanced" open>' not in html
    assert '<option value="physical" selected>' in html
    primary = html.split('<section class="workspace"', 1)[1].split(
        '<details class="advanced">', 1
    )[0]
    assert primary.count("<button") == 1
    assert "<select" not in primary
    assert 'id="shot-index"' in html


def test_javascript_controls_select_filter_zoom_and_open_matching_waveform(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable for pulse-sheet interaction checking")
    from test_artiq import make_compiled

    compiled = make_compiled()
    second = deepcopy(compiled.points[0])
    second.index = 1
    second.coordinates = {"duration": 2e-5}
    compiled.points.append(second)
    points = _payload(compiled)
    axis = execution_axis(compiled)
    gate = points[0]["logical"][0]
    gate["meta"]["waveform"] = {
        "card": "A",
        "channel": 0,
        "group": "g",
        "logical_id": "e3",
        "t0": 0,
        "t1": 10e-6,
    }
    points[1]["logical"][0]["meta"]["waveform"] = dict(gate["meta"]["waveform"])
    harness = r"""
const assert=require('node:assert/strict');
class Element {
  constructor(){this.value='';this.checked=false;this.hidden=false;
    this.listeners={};this.children=[];this.layout={};}
  addEventListener(name,fn){this.listeners[name]=fn;}
  on(name,fn){this.listeners[name]=fn;}
  appendChild(child){this.children.push(child);}
  replaceChildren(){this.children=[];}
  setAttribute(key,value){this[key]=value;}
  fire(name,event){this.listeners[name](event);}
}
const elements=new Map();
globalThis.document={getElementById(id){if(!elements.has(id))elements.set(id,
    new Element());return elements.get(id);},createElement(){return new Element();}};
const el=id=>document.getElementById(id);
globalThis.Plotly={react(graph,data,layout){graph.data=data;graph.layout=layout;},
  relayout(graph,update){for(const [key,value] of Object.entries(update)){
    const path=key.split('.');let target=graph.layout;
    for(const part of path.slice(0,-1))target=target[part];
    target[path.at(-1)]=value;}}};
el('point').value='all';el('view').value='logical';el('unit').value='µs';
    el('shot').value='all';el('shot-index').value='0';el('frames').checked=true;
const sheet=PulseSheet.mount(points,2,'cyclic','waveform.html',axis);
const graph=el('timeline');
assert.equal(graph.data.length,4);
assert.deepEqual(graph.data.map(t=>t.meta.execution_index),[0,1,2,3]);
assert.deepEqual(graph.data.map(t=>[t.meta.point_index,t.meta.shot]),[[0,0],[1,0],[0,1],[1,1]]);
assert.deepEqual(graph.data.map(t=>t.base),[[3],[17],[31],[45]]);
assert.deepEqual(graph.data.map(t=>t.meta.lane),Array(4).fill('q0.raman'));
assert.equal(graph.data[0].orientation,'h');
assert.equal(graph.layout.xaxis.minor.showgrid,true);
assert.equal(graph.layout.xaxis.minor.dtick,graph.layout.xaxis.dtick/5);
assert.equal(graph.layout.shapes.length,4);
assert.ok(el('reference').textContent.includes('not measured wall-clock time'));
assert.ok(graph.data[3].hovertemplate.split('<br>').length<=4);
assert.ok(!graph.data[3].hovertemplate.includes('Parameters'));
// A repeated point's waveform link retains local timing despite display offsets.
graph.fire('plotly_click',{points:[{customdata:graph.data[3].customdata[0]}]});
assert.equal(el('details-empty').hidden,true);
const query=new URL(el('waveform-link').href,'https://offline.invalid/').searchParams;
assert.equal(query.get('logical_id'),'e3');assert.equal(query.get('point'),'1');
assert.equal(query.get('shot'),'1');assert.equal(query.get('execution'),'3');
assert.equal(query.get('card'),'A');assert.equal(query.get('t0'),'0');
assert.equal(query.get('t1'),'0.00001');
el('focus-pulse').fire('click');assert.deepEqual(graph.layout.xaxis.range,[43.5,56.5]);
el('window-start').value='2';el('window-end').value='6';el('apply-window').fire('click');
assert.deepEqual(graph.layout.xaxis.range,[2,6]);el('zoom-in').fire('click');
assert.deepEqual(graph.layout.xaxis.range,[3,5]);
const previousMinor=graph.layout.xaxis.minor.dtick;
graph.fire('plotly_relayout',{'xaxis.range[0]':3.1,'xaxis.range[1]':3.12});
assert.ok(graph.layout.xaxis.minor.dtick<previousMinor);
el('window-start').value='8';el('window-end').value='2';el('apply-window').fire('click');
assert.deepEqual(graph.layout.xaxis.range,[3,5]);assert.ok(el('window-status').textContent);
el('zoom').fire('click');assert.equal(graph.layout.dragmode,'zoom');
el('point').value='1';el('point').fire('change');
assert.deepEqual(graph.data.map(t=>t.base),[[17],[45]]);
assert.deepEqual(graph.data.map(t=>t.meta.execution_index),[1,3]);
el('view').value='physical';el('view').fire('change');
assert.ok(graph.data.every(t=>t.meta.role!=='control'));
el('drivers').checked=true;el('drivers').fire('change');
assert.ok(graph.data.some(t=>t.meta.role==='control'));
el('unit').value='ns';el('unit').fire('change');
assert.deepEqual(graph.data.filter(t=>t.name==='dds_output').map(t=>t.base),[[17000],[45000]]);
el('search').value='missing lane';el('search').fire('input');
assert.equal(graph.data.length,0);assert.equal(el('empty-lanes').hidden,false);
el('search').value='';el('shot').value='single';el('shot-index').value='99';
el('shot').fire('change');assert.equal(el('shot-index').value,1);
assert.ok(graph.data.every(t=>t.meta.execution_index===3));
assert.equal(el('shot-index-label').hidden,false);
// Paging bounds the drawing, not the compact execution model.
el('point').value='all';el('shot').value='all';el('view').value='logical';
el('unit').value='µs';
const largeAxis={...axis,shots:123,mode:'sequential',total_executions:246};
const paged=PulseSheet.mount(points,123,'sequential','waveform.html',largeAxis);
assert.equal(graph.data.length,50);assert.equal(graph.data[49].meta.execution_index,49);
assert.ok(el('selection').textContent.includes('more executions'));
el('execution-start').value='244';el('execution-start').fire('change');
assert.deepEqual(graph.data.map(t=>t.meta.execution_index),[244,245]);
assert.deepEqual(graph.data.map(t=>t.meta.shot),[121,122]);
assert.equal(points[1].logical[0].meta.shot,undefined);
assert.deepEqual(points[1].logical[0].x,[0,10]);
const unsafe={...points[0].logical[0],meta:{...points[0].logical[0].meta,
    lane:'<img src=x>',label:'<script>alert(1)</script>'}};
const safe=PulseSheet.blockTrace(unsafe,'µs');
    assert.ok(safe.y[0].startsWith('&lt;'));
    assert.ok(!safe.text[0].includes('<script>'));
const marker={...unsafe,x:[2,2],meta:{...unsafe.meta,role:'frame'}};
    assert.equal(PulseSheet.blockTrace(marker,'µs').marker.symbol,'diamond');
console.log('pulse sheet interactions passed');
"""
    script = tmp_path / "pulse_sheet_interactions.js"
    script.write_text(
        Path("src/taiyidsl/reporting/assets/viewer_execution.js").read_text(
            encoding="utf-8"
        )
        + _APPLICATION_JS
        + "\nconst points="
        + json.dumps(points)
        + ";\nconst axis="
        + json.dumps(axis)
        + ";\n"
        + harness,
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "interactions passed" in result.stdout
