"""Streamed viewers retain DDS commands beside lazily loaded final DAC bytes."""

import json
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from taiyidsl.output_simulator import _dds_views

ROOT = Path(__file__).parents[1]


def _shard(path):
    source = path.read_text(encoding="utf-8")
    return json.loads(source.split(",", 1)[1].rsplit(");", 1)[0])


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    folder = tmp_path_factory.mktemp("paged_dds")
    example = runpy.run_path(str(ROOT / "examples/artiq_spectrum/transport_dds.py"))
    lab, experiment = example["build_demo"]()
    store = lab.compile_stream(experiment, directory=folder / "store")
    result = store.export_report(folder / "viewer")
    return store, result["index"].parent


def test_paged_shard_preserves_dds_without_packed_segments(report):
    store, output = report
    point = _shard(output / "data/chunk_0.js")["points"][0]
    compiled = store.load_chunk(0)
    assert point["dds"] == list(_dds_views(compiled, compiled.points[0]))
    assert {card["card"] for card in point["cards"]} == {"A", "B"}
    assert [card["card"] for card in point["dds"]] == ["C"]
    assert all("segments" not in card for card in point["dds"])
    expected = {
        segment["sha256"] for card in point["cards"] for segment in card["segments"]
    }
    assert {
        path.stem.removeprefix("segment_")
        for path in (output / "data").glob("segment_*.js")
    } == expected
    assert all(min(curve["amplitude"]) > 0 for curve in point["dds"][0]["curves"])
    compiled.assert_integrity()


def test_paged_mixed_render_zoom_and_dds_only_use_shared_decoder(report):
    """Run page callbacks with fake DOM/Plotly; this is not browser render QA."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the paged viewer callback test")
    _, output = report
    script = r"""
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const output = process.argv[1];
const decoder = require(path.join(output, 'decoder.js'));
const source = fs.readFileSync(path.join(output, 'data/chunk_0.js'), 'utf8');
const payload = JSON.parse(source.slice(
  source.indexOf(',')+1,source.lastIndexOf(');')));
const points = payload.points, calls = [], loads = [], elements = new Map();
class Element {
  constructor() { this.value = ''; this.textContent = ''; this.events = {}; }
  replaceChildren() { this.value = ''; }
  appendChild(child) { if (this.value === '') this.value = String(child.value); }
  addEventListener(name, callback) { this.events[name] = callback; }
  on(name, callback) { this.events[name] = callback; }
  remove() {}
}
for (const id of ['paged-plan','chunk','point','channel','start','duration',
  'render','full-sequence','wave','metadata','notice']) {
  elements.set(id,new Element());
}
elements.get('paged-plan').textContent = '[{"index":0,"points":[0]}]';
const context = {TaiyiOutputSimulator:decoder, Promise, console,
  Plotly:{react:async (id,traces,layout) => {calls.push({traces,layout});},
    purge:() => {}, restyle:() => {}},
  document:{getElementById:id => elements.get(id), createElement:() => new Element(),
    head:{appendChild:script => {
      loads.push(script.src);
      queueMicrotask(() => {
        vm.runInContext(fs.readFileSync(path.join(output,script.src),'utf8'),context);
        script.onload();
      });
    }}}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(output,'paged.js'),'utf8'),context);
async function ready(count) {
  for (let i=0;i<100;i++) {
    await new Promise(resolve=>setImmediate(resolve));
    const finished=elements.get('notice').textContent.includes(
      'Not hardware acceptance');
    if (calls.length>=count && finished) return;
  }
  throw new Error(elements.get('notice').textContent);
}
(async()=>{
  await ready(1);
  const full=calls.at(-1);
  const cOption=JSON.stringify(['dds',0,0]);
  elements.get('channel').value=cOption; elements.get('channel').events.change();
  const metadata=JSON.parse(elements.get('metadata').textContent);
  const beforeZoom=loads.length;
  elements.get('wave').events.plotly_relayout({'xaxis.range':[220,221]});
  await ready(2);
  const zoom=calls.at(-1);
  const ddsOnly={...points[0],cards:[]};
  const requested=context.TaiyiPaged.segmentRequests(ddsOnly,
    {start_s:0,duration_s:.001});
  const result=await decoder.replayExecutions({points:[ddsOnly],packed:{}},
    [{point:ddsOnly.index,ordinal:0,shot:0,offset_s:0}],
    context.TaiyiPaged.pointWindow(ddsOnly));
  process.stdout.write(JSON.stringify({full,zoom,metadata,
    zoomLoads:loads.slice(beforeZoom),ddsOnly:{requested,hasDDS:result.hasDDS,
      traces:result.traces,vertices:result.ddsVertices}}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    process = subprocess.run(
        [node, "-e", script, str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    for render in (result["full"], result["zoom"]):
        assert {trace.get("yaxis", "y") for trace in render["traces"]} == {
            "y",
            "y2",
            "y3",
        }
        assert all(trace.get("xaxis", "x") == "x" for trace in render["traces"])
        assert render["layout"]["xaxis"]["anchor"] == "free"
        assert render["layout"]["yaxis"]["domain"][0] > 0.5
    assert result["zoom"]["layout"]["xaxis"]["range"] == pytest.approx([220, 221])
    assert result["metadata"]["card"] == "C"
    assert result["metadata"]["cores"] == [0, 1]
    assert "no RF phase" in result["metadata"]["model"]
    for trace in result["zoom"]["traces"]:
        if trace.get("yaxis") == "y3":
            assert all(value > 0 for value in trace["y"] if value is not None)
    assert all(name.startswith("data/segment_") for name in result["zoomLoads"])
    assert result["ddsOnly"]["requested"] == []
    assert result["ddsOnly"]["hasDDS"]
    assert result["ddsOnly"]["vertices"] > 0
    assert {trace["yaxis"] for trace in result["ddsOnly"]["traces"]} == {"y2", "y3"}
