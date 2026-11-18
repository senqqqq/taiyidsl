/* Local classic-script shards work under file:// without fetch or module CORS. */
(function(root) {
  "use strict";
  const pending = new Map();
  const api = {
    register(key, value) {
      const request = pending.get(key);
      if (request) { request.settled = true; request.resolve(value); }
    },
    load(key, path) {
      if (pending.has(key)) return pending.get(key).promise;
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      const script = document.createElement("script");
      const request = {promise, resolve, reject, settled: false};
      pending.set(key, request);
      script.src = path;
      script.onerror = () => reject(new Error("Missing local report data: " + path));
      script.onload = () => { if (!request.settled) reject(new Error("Report shard did not register its declared data: " + path)); };
      document.head.appendChild(script);
      return promise.finally(() => { script.remove(); pending.delete(key); });
    },
    segmentRequests(point, options) {
      const simulator = root.TaiyiOutputSimulator;
      const start = Number(options.start_s), duration = Number(options.duration_s);
      if (!Number.isFinite(start) || !Number.isFinite(duration) || duration <= 0) {
        throw new Error("Choose a finite start and a positive window length.");
      }
      const a = simulator.fraction(options.start_s), b = simulator.fraction(options.duration_s);
      const end = [a[0] * b[1] + b[0] * a[1], a[1] * b[1]];
      const hashes = new Set();
      for (const view of point.cards) {
        const first = Math.max(0, simulator.sampleCeil(a, view));
        const last = Math.min(view.physical_duration_samples, simulator.sampleCeil(end, view));
        if (last <= first) continue;
        for (const span of simulator.walk(view)) {
          if (span.start < last && span.end > first) hashes.add(span.segment.sha256);
        }
      }
      return [...hashes];
    },
    pointWindow(point) {
      const bounds = [];
      for (const view of point.cards) bounds.push(view.sample0_time_s,
        view.sample0_time_s + view.physical_duration_samples / view.sample_rate_hz);
      for (const view of point.dds || []) bounds.push(view.command_start_s, view.command_end_s);
      if (!bounds.length) return {start_s: "0", duration_s: "0.000001"};
      const start = Math.min(...bounds), end = Math.max(...bounds);
      return {start_s: String(start), duration_s: String(Math.max(end - start, 1e-9))};
    }
  };
  root.TaiyiPaged = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined") return;
  const plan = JSON.parse(document.getElementById("paged-plan").textContent);
  const $ = id => document.getElementById(id);
  let chunk = null, generation = 0, current = null, plotEventsBound = false;
  const hiddenChannels = new Set();
  const fill = (select, items) => {
    select.replaceChildren();
    for (const [value, label] of items) {
      const option = document.createElement("option");
      option.value = value; option.textContent = label; select.appendChild(option);
    }
  };
  function selectedPoint() { return chunk.points[Number($("point").value)]; }
  function selected() {
    const point = selectedPoint();
    if (!$("channel").value) return {point};
    const [mode, card, channel] = JSON.parse($("channel").value);
    return {point, mode, view: (mode === "dds" ? point.dds : point.cards)[card], channel};
  }
  function setWindow(window) {
    const fields = TaiyiOutputSimulator.formatWindow(window.start_s, window.duration_s);
    $("start").value = fields.start_us; $("duration").value = fields.duration_us;
  }
  function fitSelection() { setWindow(api.pointWindow(selectedPoint())); }
  function metadata() {
    const {view, channel, mode} = selected();
    if (!view) { $("metadata").textContent = ""; return; }
    const data = mode === "dds" ? {
      model: "Quantized DDS command prediction, no RF phase or DAC samples",
      group: view.group, card: view.card, channel,
      trigger_time_s: view.trigger_time_s, trigger_latency_ns: view.trigger_latency_ns,
      command_start_s: view.command_start_s, command_end_s: view.command_end_s,
      hold_start_s: view.hold_start_s, hold_end_s: view.hold_end_s,
      cores: view.curves.filter(curve => curve.channel === channel).map(curve => curve.core)
    } : view.pulses.filter(p => p.channel === channel);
    $("metadata").textContent = JSON.stringify(data, null, 2);
  }
  function pointChanged() {
    const point = selectedPoint();
    const channels = [];
    point.cards.forEach((view, index) => view.active_channels.forEach(channel => {
      channels.push([JSON.stringify(["awg", index, channel]), `${view.group} / ${view.card} / CH${channel} · AWG`]);
    }));
    (point.dds || []).forEach((view, index) => view.active_channels.forEach(channel => {
      channels.push([JSON.stringify(["dds", index, channel]), `${view.group} / ${view.card} / CH${channel} · DDS`]);
    }));
    fill($("channel"), channels);
    metadata(); fitSelection();
    render();
  }
  async function render() {
    const token = ++generation;
    try {
      const point = selectedPoint();
      const window = TaiyiOutputSimulator.inputWindow($("start").value, $("duration").value);
      const packed = {};
      $("notice").textContent = "Loading only intersecting packed segments…";
      for (const hash of api.segmentRequests(point, window)) {
        packed[hash] = await api.load("segment:" + hash, "data/segment_" + hash + ".js");
        if (token !== generation) return;
      }
      const result = await TaiyiOutputSimulator.replayExecutions(
        {points: [point], packed, max_samples: 100000},
        [{point: point.index, ordinal: 0, shot: 0, offset_s: 0}], window,
        {units: "voltage", cancelled: () => token !== generation});
      if (token !== generation) return;
      for (const trace of result.traces) trace.visible = hiddenChannels.has(trace.uid) ? "legendonly" : true;
      const start = Number(window.start_s), duration = Number(window.duration_s);
      await Plotly.react("wave", result.traces, {
        margin:{l:90,r:25,t:45,b:110}, hovermode:"closest", dragmode:"zoom",
        legend:{orientation:"h",y:-0.18,itemdoubleclick:false},
        xaxis:{title:{text:"Local shot / control T0 (µs)"},anchor:"free",position:0,
          range:[start * 1e6, (start + duration) * 1e6]},
        ...TaiyiOutputSimulator.outputAxes("voltage", result.hasDDS, point.cards.length > 0),
        title:{text:`Point ${point.index} · all card channels · representative shot`}
      }, {responsive:true,displaylogo:false,scrollZoom:true,
        modeBarButtonsToRemove:["select2d","lasso2d","autoScale2d"]});
      if (token !== generation) return;
      current = {start, duration, result};
      if (!plotEventsBound) {
        $("wave").on("plotly_relayout", onRelayout);
        $("wave").on("plotly_legendclick", event => {
          const trace = current?.result.traces[event.curveNumber];
          if (!trace) return false;
          if (hiddenChannels.has(trace.uid)) hiddenChannels.delete(trace.uid); else hiddenChannels.add(trace.uid);
          trace.visible = hiddenChannels.has(trace.uid) ? "legendonly" : true;
          Plotly.restyle("wave", {visible:trace.visible}, [event.curveNumber]);
          return false;
        });
        plotEventsBound = true;
      }
      const awg = result.overview ? `${result.bins} exact DAC min/max bins; zoom for individual samples` : `${result.exact} exact DAC samples`;
      const dds = result.hasDDS ? ` DDS: ${result.ddsVertices} command vertices; frequency MHz / amplitude FS. Hold continues; unknown host/shot gaps have no assigned duration.` : "";
      $("notice").textContent = `${awg}; ${Object.keys(packed).length} packed segments loaded.${dds} Not hardware acceptance.`;
    } catch (error) { if (token === generation) $("notice").textContent = error.message; }
  }
  function onRelayout(event) {
    if (!current) return;
    if (event["xaxis.autorange"]) {fitSelection(); render(); return;}
    const range = event["xaxis.range"] || [event["xaxis.range[0]"], event["xaxis.range[1]"]];
    if (!range.every(Number.isFinite) || range[1] <= range[0]) return;
    const window = TaiyiOutputSimulator.queryWindow(
      TaiyiOutputSimulator.inputWindow(range[0], 0).start_s,
      TaiyiOutputSimulator.inputWindow(range[1], 0).start_s);
    setWindow(window);
    if (Number(window.start_s) !== current.start || Number(window.duration_s) !== current.duration) render();
  }
  async function chunkChanged() {
    const token = ++generation;
    try {
      const index = Number($("chunk").value);
      chunk = null; current = null; plotEventsBound = false;
      $("metadata").textContent = "";
      Plotly.purge("wave");
      const next = await api.load("chunk:" + index, "data/chunk_" + index + ".js");
      if (token !== generation) return;
      chunk = next;
      fill($("point"), chunk.points.map((point, index) => [index, `${point.index}: ${JSON.stringify(point.coordinates)}`]));
      pointChanged();
    } catch (error) { $("notice").textContent = error.message; }
  }
  fill($("chunk"), plan.map(item => [item.index, `${item.index} (${item.points.length} points)`]));
  $("chunk").addEventListener("change", chunkChanged);
  $("point").addEventListener("change", pointChanged);
  $("channel").addEventListener("change", metadata);
  $("render").addEventListener("click", render);
  $("full-sequence").addEventListener("click", () => {fitSelection(); render();});
  chunkChanged();
})(typeof globalThis !== "undefined" ? globalThis : this);
