/* Exact offline replay of packed little-endian DAC bytes. No RF regeneration. */
(function (root) {
  "use strict";
  const CACHE_LIMIT = 16 * 1024 * 1024;
  const decodedCache = new Map();
  let cacheBytes = 0;

  function traceUid(...parts) {
    // Plotly uses uid in CSS selectors while removing old traces/colorbars.
    // Encode every UTF-16 code unit: stable, collision-free, CSS-safe, and
    // independent of zoom/trace order. Keep semantic keys in legendgroup.
    const value = JSON.stringify(parts);
    let encoded = "t";
    for (let i = 0; i < value.length; i++) encoded += value.charCodeAt(i).toString(16).padStart(4, "0");
    return encoded;
  }

  function fraction(value) {
    if (Array.isArray(value)) return value.map(BigInt);
    if (!Number.isFinite(Number(value))) throw new Error("Time must be finite.");
    const parts = String(value).toLowerCase().split("e");
    const exponent = Number(parts[1] || 0);
    const mantissa = parts[0].split(".");
    let numerator = BigInt(mantissa.join(""));
    const scale = (mantissa[1] || "").length - exponent;
    let denominator = 1n;
    if (scale > 0) denominator = 10n ** BigInt(scale);
    else numerator *= 10n ** BigInt(-scale);
    return [numerator, denominator];
  }

  function add(a, b) { return [a[0] * b[1] + b[0] * a[1], a[1] * b[1]]; }
  function subtract(a, b) { return add(a, [-b[0], b[1]]); }
  function multiply(a, b) { return [a[0] * b[0], a[1] * b[1]]; }
  function greater(a, b) { return a[0] * b[1] > b[0] * a[1]; }
  function decimal(value) {
    const [numerator, denominator] = fraction(value);
    if (denominator <= 0n) throw new Error("Invalid time denominator.");
    const negative = numerator < 0n;
    const absolute = negative ? -numerator : numerator;
    let result = String(absolute / denominator), remainder = absolute % denominator;
    if (remainder) result += ".";
    let digits = 0;
    while (remainder) {
      if (++digits > 400) throw new Error("Time does not have a bounded decimal representation.");
      remainder *= 10n;
      result += String(remainder / denominator);
      remainder %= denominator;
    }
    return negative ? "-" + result : result;
  }
  function inputWindow(startUs, durationUs) {
    return {
      start_s: decimal(multiply(fraction(startUs), [1n, 1000000n])),
      duration_s: decimal(multiply(fraction(durationUs), [1n, 1000000n]))
    };
  }
  function formatWindow(start, duration) {
    return {
      start_us: decimal(multiply(fraction(start), [1000000n, 1n])),
      duration_us: decimal(multiply(fraction(duration), [1000000n, 1n]))
    };
  }
  function queryWindow(start, stop) {
    return {start_s: decimal(fraction(start)), duration_s: decimal(subtract(fraction(stop), fraction(start)))};
  }
  function pulseWindow(view, pulse) {
    const rate = fraction(view.sample_rate_hz);
    return {
      start_s: decimal(add(fraction(view.sample0_fraction), [BigInt(pulse.start_sample) * rate[1], rate[0]])),
      duration_s: decimal([BigInt(pulse.duration_samples) * rate[1], rate[0]])
    };
  }
  function sequenceWindow(views) {
    if (!views.length) return {start_s: "0", duration_s: "0.000001"};
    let first = fraction(views[0].sample0_fraction), last = first;
    for (const view of views) {
      const start = fraction(view.sample0_fraction);
      const end = add(start, [BigInt(view.physical_duration_samples), BigInt(view.sample_rate_hz)]);
      if (greater(first, start)) first = start;
      if (greater(end, last)) last = end;
    }
    return {start_s: decimal(first), duration_s: decimal(subtract(last, first))};
  }
  function previewWindow(view, pulse, maxSamples = 100000, edge = false) {
    const length = pulse.duration_samples, rate = view.sample_rate_hz;
    let count = length;
    if (length > Math.min(maxSamples, Math.round(rate * 2e-6))) {
      const cycles = pulse.frequency_hz >= 1e6 ? 12 : 2;
      count = pulse.frequency_hz > 0 ? Math.ceil(cycles * rate / pulse.frequency_hz) : Math.round(rate * 2e-6);
      count = Math.min(length, maxSamples, Math.max(32, count));
    }
    count = Math.min(count, maxSamples);
    const centered = !edge && count < length && pulse.envelope.kind === "flat_top_gaussian";
    const offset = centered ? Math.floor((length - count) / 2) : 0;
    return {...pulseWindow(view, {...pulse, start_sample: pulse.start_sample + offset, duration_samples: count}),
      start_sample: pulse.start_sample + offset, sample_count: count,
      position: count === length ? "complete pulse" : centered ? "pulse center" : "pulse start"};
  }
  function windowSummary(view, channel, result) {
    let nonzero = 0, peak = 0;
    for (const value of result.codes) {
      if (value !== 0) nonzero++;
      peak = Math.max(peak, Math.abs(value));
    }
    const overlaps = view.pulses.filter(pulse => pulse.channel === channel &&
      pulse.start_sample < result.last && pulse.start_sample + pulse.duration_samples > result.first);
    return {nonzero, peak, overlaps: overlaps.length,
      status: !result.codes.length ? "outside_program" : nonzero ? "nonzero" : overlaps.length ? "zero_codes" : "idle"};
  }
  function ceilDivide(a, b) {
    const quotient = a / b;
    return a > 0n && a % b !== 0n ? quotient + 1n : quotient;
  }

  function sampleCeil(time, view) {
    const origin = view.sample0_fraction.map(BigInt);
    const rate = fraction(view.sample_rate_hz);
    const value = ceilDivide(
      (time[0] * origin[1] - origin[0] * time[1]) * rate[0],
      time[1] * origin[1] * rate[1]
    );
    const result = Number(value);
    if (!Number.isSafeInteger(result)) throw new Error("Window exceeds exact integer timing range.");
    return result;
  }

  function walk(view) {
    const steps = new Map(view.steps.map(step => [step.index, step]));
    const segments = new Map(view.segments.map(segment => [segment.id, segment]));
    if (steps.size !== view.steps.length || segments.size !== view.segments.length) {
      throw new Error("Duplicate sequence indices.");
    }
    const seen = new Set(), spans = [];
    let index = 0, position = 0;
    while (true) {
      if (seen.has(index) || !steps.has(index)) throw new Error("Invalid sequence next_step.");
      const step = steps.get(index), segment = segments.get(step.segment_id);
      if (!segment || !Number.isSafeInteger(step.loops) || step.loops < 1) {
        throw new Error("Invalid segment or finite loop count.");
      }
      seen.add(index);
      const end = position + segment.sample_count * step.loops;
      if (!Number.isSafeInteger(end)) throw new Error("Sequence duration exceeds exact range.");
      spans.push({start: position, end, step, segment});
      position = end;
      if (step.end) break;
      index = step.next_step;
    }
    if (seen.size !== steps.size || position !== view.physical_duration_samples) {
      throw new Error("Unreachable steps or inconsistent physical duration.");
    }
    return spans;
  }

  async function unpack(digest, packed) {
    if (decodedCache.has(digest)) {
      const value = decodedCache.get(digest);
      decodedCache.delete(digest);
      decodedCache.set(digest, value);
      return value;
    }
    const item = packed[digest];
    if (!item) throw new Error("Missing packed segment.");
    if (item.encoding === "zero") return null;
    if (item.encoding !== "zlib-base64") throw new Error("Unsupported packed encoding.");
    let bytes;
    if (typeof module !== "undefined" && module.exports) {
      bytes = require("node:zlib").inflateSync(Buffer.from(item.data, "base64"));
    } else {
      if (typeof DecompressionStream === "undefined") {
        throw new Error("This offline file needs a browser with DecompressionStream support.");
      }
      const compressed = Uint8Array.from(atob(item.data), character => character.charCodeAt(0));
      const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream("deflate"));
      bytes = new Uint8Array(await new Response(stream).arrayBuffer());
    }
    if (bytes.byteLength !== item.byte_length) throw new Error("Packed segment length mismatch.");
    // A newer render may have finished the same decompression while we awaited
    // the stream. Do not count the same cached buffer twice.
    if (decodedCache.has(digest)) return decodedCache.get(digest);
    const result = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (bytes.byteLength <= CACHE_LIMIT) {
      while (cacheBytes + bytes.byteLength > CACHE_LIMIT && decodedCache.size) {
        const key = decodedCache.keys().next().value;
        cacheBytes -= decodedCache.get(key).byteLength;
        decodedCache.delete(key);
      }
      decodedCache.set(digest, result);
      cacheBytes += bytes.byteLength;
    }
    return result;
  }

  async function decodeWindow(view, packed, options) {
    const start = Number(options.start_s), duration = Number(options.duration_s);
    const limit = options.max_samples === undefined ? 100000 : options.max_samples;
    if (!Number.isFinite(start) || !Number.isFinite(duration) || duration <= 0) {
      throw new Error("Choose a finite start and a positive window length.");
    }
    if (!Number.isSafeInteger(limit) || limit < 1) throw new Error("Invalid sample limit.");
    const channel = view.active_channels.indexOf(options.channel);
    if (channel < 0) throw new Error("Channel is not active on this card.");
    let first = sampleCeil(fraction(options.start_s), view);
    let last = sampleCeil(add(fraction(options.start_s), fraction(options.duration_s)), view);
    if (last - first > limit) {
      throw new Error(`Window needs ${last - first} samples/channel; limit ${limit}. Choose a shorter window.`);
    }
    first = Math.max(0, Math.min(view.physical_duration_samples, first));
    last = Math.max(0, Math.min(view.physical_duration_samples, last));
    const length = last - first;
    const codes = new Int16Array(length), indices = new Float64Array(length);
    const times = new Float64Array(length), segmentIds = new Int32Array(length);
    const stepIds = new Int32Array(length);
    const spans = walk(view);
    for (const span of spans) {
      const begin = Math.max(first, span.start), end = Math.min(last, span.end);
      if (begin >= end) continue;
      const samples = await unpack(span.segment.sha256, packed);
      const width = view.active_channels.length;
      if (packed[span.segment.sha256].byte_length !== span.segment.sample_count * width * 2) {
        throw new Error("Segment layout does not match active channels.");
      }
      for (let index = begin; index < end; index++) {
        const target = index - first;
        const offset = (index - span.start) % span.segment.sample_count;
        codes[target] = samples ? samples.getInt16((offset * width + channel) * 2, true) : 0;
        indices[target] = index;
        times[target] = view.sample0_time_s + index / view.sample_rate_hz;
        segmentIds[target] = span.segment.id;
        stepIds[target] = span.step.index;
      }
    }
    return {codes, indices, times, segmentIds, stepIds, first, last};
  }

  async function overviewWindow(view, packed, options) {
    // Exact extrema of final DAC bytes in contiguous bins, not sparse RF
    // sampling. Finite segment loops are reduced without expanding long waits.
    const channel = view.active_channels.indexOf(options.channel);
    if (channel < 0) throw new Error("Channel is not active on this card.");
    if (!Number.isFinite(Number(options.start_s)) || !Number.isFinite(Number(options.duration_s)) || Number(options.duration_s) <= 0) {
      throw new Error("Choose a finite start and a positive window length.");
    }
    const bins = options.max_bins === undefined ? 1600 : options.max_bins;
    if (!Number.isSafeInteger(bins) || bins < 1) throw new Error("Invalid overview bin count.");
    const first = Math.max(0, Math.min(view.physical_duration_samples, sampleCeil(fraction(options.start_s), view)));
    const last = Math.max(first, Math.min(view.physical_duration_samples, sampleCeil(add(fraction(options.start_s), fraction(options.duration_s)), view)));
    const width = view.active_channels.length, spans = walk(view), fullRanges = new Map();
    const count = Math.min(bins, last - first), starts = [], stops = [], minimum = [], maximum = [];
    let spanIndex = 0;
    async function extrema(segment, begin, end) {
      const item = packed[segment.sha256];
      if (!item || item.byte_length !== segment.sample_count * width * 2) throw new Error("Segment layout does not match active channels.");
      if (item.encoding === "zero") return [0, 0];
      const full = begin === 0 && end === segment.sample_count;
      if (full && fullRanges.has(segment.sha256)) return fullRanges.get(segment.sha256);
      const samples = await unpack(segment.sha256, packed);
      let low = 32767, high = -32768;
      for (let index = begin; index < end; index++) {
        const value = samples.getInt16((index * width + channel) * 2, true);
        low = Math.min(low, value); high = Math.max(high, value);
      }
      const result = [low, high];
      if (full) fullRanges.set(segment.sha256, result);
      return result;
    }
    // Refine the regular grid at pulse boundaries, retaining exact extrema.
    const edges = new Set([first, last]);
    for (let bin = 1; bin < count; bin++) edges.add(first + Number(BigInt(bin) * BigInt(last - first) / BigInt(count)));
    for (const boundary of options.boundaries || []) {
      if (Number.isSafeInteger(boundary) && boundary > first && boundary < last) edges.add(boundary);
    }
    const ordered = [...edges].sort((a, b) => a - b);
    for (let bin = 0; bin + 1 < ordered.length; bin++) {
      const begin = ordered[bin], end = ordered[bin + 1];
      let low = 32767, high = -32768;
      while (spanIndex < spans.length && spans[spanIndex].end <= begin) spanIndex++;
      for (let index = spanIndex; index < spans.length && spans[index].start < end; index++) {
        const span = spans[index], size = span.segment.sample_count;
        const left = Math.max(begin, span.start) - span.start, right = Math.min(end, span.end) - span.start;
        const start = left % size, length = right - left;
        const ranges = length >= size ? [[0, size]] : start + length <= size ? [[start, start + length]] : [[start, size], [0, (start + length) % size]];
        for (const [a, b] of ranges) {
          const [lo, hi] = await extrema(span.segment, a, b);
          low = Math.min(low, lo); high = Math.max(high, hi);
        }
      }
      starts.push(begin); stops.push(end); minimum.push(low); maximum.push(high);
    }
    return {overview: true, first, last, starts, stops, minimum, maximum,
      codes: minimum.flatMap((value, index) => [value, maximum[index]])};
  }

  function channelInventory(points) {
    const found = new Map();
    for (const point of points) for (const view of point.cards) {
      for (const channel of view.active_channels) {
        const key = JSON.stringify([view.group, view.card, channel]);
        found.set(key, {key, group: view.group, card: view.card, channel});
      }
    }
    for (const point of points) for (const event of point.virtual_z || []) {
      for (const output of event.channels || []) {
        const key = JSON.stringify([output.group, output.card, output.channel]);
        if (!found.has(key)) found.set(key, {...output, key});
      }
    }
    const palette = ["#167c79", "#d47724", "#7657b5", "#c13b58", "#337bb1", "#74933e", "#b56b9b", "#805b39"];
    return [...found.values()].sort((a, b) => a.key.localeCompare(b.key)).map((item, index) => ({
      ...item, color: palette[index] || `hsl(${(index * 137.508) % 360} 62% 40%)`,
      name: `${item.group} / ${item.card} / CH ${item.channel}`
    }));
  }

  function ddsWindow(view, options) {
    // These are sparse command vertices, not DAC samples. Interpolate only
    // between distinct-time vertices; duplicate times preserve explicit pins.
    const start = Number(options.start_s), duration = Number(options.duration_s);
    if (!Number.isFinite(start) || !Number.isFinite(duration) || duration <= 0) {
      throw new Error("Choose a finite start and a positive window length.");
    }
    const curves = [];
    let vertices = 0;
    for (const curve of view.curves) {
      if (options.core !== undefined && curve.core !== options.core) continue;
      const times = curve.time_s, count = times.length;
      if (!count) continue;
      const left = Math.max(start, times[0]), right = Math.min(start + duration, times[count - 1]);
      if (right < left || times[count - 1] < start || times[0] > start + duration) continue;
      const result = {core: curve.core, channel: curve.channel,
        time_s: [], frequency_hz: [], amplitude: [], frame: [], stage: []};
      function append(time, index, ratio = 0, interpolated = false) {
        result.time_s.push(time);
        for (const field of ["frequency_hz", "amplitude"]) {
          result[field].push(interpolated ? curve[field][index] + ratio * (curve[field][index + 1] - curve[field][index]) : curve[field][index]);
        }
        result.frame.push(curve.frame[index]);
        result.stage.push(interpolated ? `window_clip:${curve.stage[index]}` : curve.stage[index]);
      }
      for (let index = 0; index < count; index++) {
        const time = times[index];
        if (time >= left && time <= right) append(time, index);
        if (index + 1 >= count || times[index + 1] <= time) continue;
        for (const boundary of left === right ? [left] : [left, right]) {
          if (time < boundary && boundary < times[index + 1]) {
            append(boundary, index, (boundary - time) / (times[index + 1] - time), true);
          }
        }
      }
      // A clipped left edge can be inserted after a prior vertex; stable sort
      // keeps all coincident commit vertices in their original command order.
      const order = result.time_s.map((_, index) => index).sort((a, b) => result.time_s[a] - result.time_s[b]);
      for (const field of ["time_s", "frequency_hz", "amplitude", "frame", "stage"]) result[field] = order.map(index => result[field][index]);
      if (result.time_s.length) { curves.push(result); vertices += result.time_s.length; }
    }
    return {curves, vertices};
  }

  function ddsInventory(points) {
    const found = new Map(), palette = ["#337bb1", "#d47724", "#167c79", "#7657b5", "#c13b58", "#74933e"];
    for (const point of points) for (const view of point.dds || []) for (const curve of view.curves) {
      const key = JSON.stringify([view.group, view.card, curve.channel, curve.core]);
      found.set(key, {key, group: view.group, card: view.card, channel: curve.channel, core: curve.core});
    }
    return [...found.values()].sort((a, b) => a.key.localeCompare(b.key)).map((item, index) => ({
      ...item, color: palette[index] || `hsl(${(index * 137.508) % 360} 62% 40%)`,
      name: `${item.group} / ${item.card} / CH ${item.channel} / tone ${item.core}`
    }));
  }

  function emptyDDSTraces(inventory) {
    return inventory.flatMap(item => ["frequency", "amplitude"].map(quantity => ({
      type: "scatter", mode: "lines", x: [], y: [], customdata: [],
      name: `${item.name} · ${quantity === "frequency" ? "MHz" : "amplitude"}`,
      uid: traceUid("dds", item.key, quantity), legendgroup: `${item.key}:${quantity}`,
      yaxis: quantity === "frequency" ? "y2" : "y3", showlegend: true, connectgaps: false,
      line: {color: item.color, width: 1.6, simplify: false, shape: "linear"},
      hovertemplate: "Time %{x:.9f} µs<br>Execution %{customdata[0]} · point %{customdata[1]} · shot %{customdata[2]}<br>%{customdata[3]}<extra>%{fullData.name}</extra>"
    })));
  }

  function ddsTraces(views, options) {
    const inventory = options.inventory || ddsInventory([{dds: views}]);
    const traces = emptyDDSTraces(inventory), byKey = new Map(traces.map(trace => [trace.legendgroup, trace]));
    const offset = Number(options.offset_s || 0), entry = options.entry || {ordinal: 0, point: 0, shot: 0};
    let vertices = 0;
    for (const view of views) {
      const result = ddsWindow(view, options);
      vertices += result.vertices;
      for (const curve of result.curves) {
        const key = JSON.stringify([view.group, view.card, curve.channel, curve.core]);
        for (const quantity of ["frequency", "amplitude"]) {
          const trace = byKey.get(`${key}:${quantity}`);
          const values = quantity === "frequency" ? curve.frequency_hz.map(value => value / 1e6) : curve.amplitude;
          for (let index = 0; index < values.length; index++) {
            trace.x.push((curve.time_s[index] + offset) * 1e6);
            trace.y.push(values[index]);
            trace.customdata.push([entry.ordinal, entry.point, entry.shot,
              `DDS command prediction · frame ${curve.frame[index]} · ${curve.stage[index]}<br>${curve.frequency_hz[index].toPrecision(12)} Hz · amplitude ${curve.amplitude[index].toPrecision(8)} FS<br>Local T0 + ${(curve.time_s[index] * 1e6).toFixed(9)} µs; no RF phase reconstruction`]);
          }
          trace.x.push(null); trace.y.push(null); trace.customdata.push(null);
        }
      }
    }
    return {traces, inventory, vertices};
  }

  function outputAxes(units, hasDDS, hasAWG = true) {
    const common = {gridcolor: "#e2e5dc", zerolinecolor: "#939b92", anchor: "x", automargin: true};
    const title = units === "codes" ? "AWG · DAC code" : units === "voltage" ? "AWG · nominal V" : "AWG · normalized FS";
    const virtualAxis = {overlaying: "y", range: [0, 1], visible: false, fixedrange: true, anchor: "x"};
    if (!hasDDS) return {yaxis: {...common, title: {text: title}}, yaxis4: virtualAxis};
    return {
      yaxis: {...common, title: {text: title}, domain: hasAWG ? [.72, 1] : [0, 1], visible: hasAWG},
      yaxis2: {...common, title: {text: "DDS frequency (MHz)"}, domain: hasAWG ? [.36, .64] : [.55, 1]},
      yaxis3: {...common, title: {text: "DDS amplitude (FS)"}, domain: hasAWG ? [0, .28] : [0, .45]},
      yaxis4: virtualAxis
    };
  }

  const PULSE_COLORS = ["#337bb1", "#d47724", "#7657b5", "#16867a", "#c13b58", "#798d28", "#b35c98", "#805b39", "#009ac7", "#b38f00", "#9b3a35", "#505fb0"];
  const IDLE_COLOR = "#929b9f", OVERLAP_COLOR = "#454b52";
  const channelKey = (view, channel) => JSON.stringify([view.group, view.card, channel]);

  function pulseAnnotations(point) {
    // Number occurrences over the whole point, not the visible window or shot.
    // A pulse crossing hardware segments retains one identity.
    const byView = new Map(point.cards.map(view => [view, new Map()])), channels = new Map();
    for (const view of point.cards) for (const pulse of view.pulses || []) {
      if (!(pulse.duration_samples > 0)) continue;
      const key = channelKey(view, pulse.channel);
      if (!channels.has(key)) channels.set(key, []);
      channels.get(key).push({view, pulse, start: view.sample0_time_s + pulse.start_sample / view.sample_rate_hz});
    }
    for (const items of channels.values()) {
      items.sort((a, b) => a.start - b.start);
      items.forEach(({view, pulse}, index) => {
        const map = byView.get(view);
        if (!map.has(pulse.channel)) map.set(pulse.channel, []);
        map.get(pulse.channel).push({pulse, index});
      });
    }
    return byView;
  }

  function pulseSpans(items, first, last) {
    const events = new Map([[first, {add: [], remove: []}], [last, {add: [], remove: []}]]);
    for (const item of items) {
      const a = Math.max(first, item.pulse.start_sample), b = Math.min(last, item.pulse.start_sample + item.pulse.duration_samples);
      if (b <= a) continue;
      for (const edge of [a, b]) if (!events.has(edge)) events.set(edge, {add: [], remove: []});
      events.get(a).add.push(item); events.get(b).remove.push(item.index);
    }
    const edges = [...events.keys()].sort((a, b) => a - b), active = new Map(), spans = [];
    for (let i = 0; i + 1 < edges.length; i++) {
      const event = events.get(edges[i]);
      for (const index of event.remove) active.delete(index);
      for (const item of event.add) active.set(item.index, item);
      const members = [...active.values()].sort((a, b) => a.index - b.index);
      spans.push({first: edges[i], last: edges[i + 1], items: members,
        kind: members.length === 1 ? "pulse" : members.length ? "overlap" : "idle",
        pulse_indices: members.map(item => item.index)});
    }
    return spans;
  }

  function pulseColor(span) {
    if (span.kind === "idle") return IDLE_COLOR;
    if (span.kind === "overlap") return OVERLAP_COLOR;
    const index = span.pulse_indices[0];
    return PULSE_COLORS[index] || `hsl(${(index * 137.508) % 360}, 62%, 40%)`;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
  }

  function gateDescription(annotations = []) {
    return annotations.map(gate => `Gate ${escapeHtml(gate.native_gate)} · Clifford #${escapeHtml(gate.clifford_index)} · ${gate.recovery ? "RECOVERY" : `random (position ${Number(gate.clifford_position) + 1})`} · step ${Number(gate.implementation_step) + 1}`).join("<br>");
  }

  function pulseDescription(span, view) {
    if (span.kind === "idle") return "Outside declared pulse intervals";
    const details = span.items.map(({pulse, index}) => {
      const start = view.sample0_time_s + pulse.start_sample / view.sample_rate_hz;
      const duration = pulse.duration_samples / view.sample_rate_hz;
      const phase = ((pulse.phase_rad * 180 / Math.PI) % 360 + 360) % 360;
      const gate = gateDescription(pulse.gate_annotations);
      return (gate ? gate + "<br>" : "") + `Pulse ${index + 1} · local T0 ${Number((start * 1e6).toPrecision(12))}–${Number(((start + duration) * 1e6).toPrecision(12))} µs · duration ${Number((duration * 1e6).toPrecision(12))} µs · RF phase ${Number(phase.toPrecision(10))}°`;
    });
    return (span.kind === "overlap" ? "Overlapping pulse annotations; showing final DAC<br>" : "") + details.join("<br>");
  }

  function toggleChannel(traces, hidden, selected) {
    const key = selected.legendgroup || selected.uid;
    if (hidden.has(key)) hidden.delete(key); else hidden.add(key);
    const visible = hidden.has(key) ? "legendonly" : true, indices = [];
    traces.forEach((trace, index) => {
      if ((trace.legendgroup || trace.uid) === key) {trace.visible = visible; indices.push(index);}
    });
    return {visible, indices};
  }

  function virtualZTraces(points, entries, window, inventory) {
    const byPoint = new Map(points.map(point => [point.index, point]));
    const channels = new Map(inventory.map(item => [item.key, item]));
    const groups = new Map(), traces = new Map();
    const left = Number(window.start_s), right = left + Number(window.duration_s);
    let count = 0;
    for (const entry of entries) {
      const offset = Number(decimal(fraction(entry.offset_fraction || entry.offset_s || 0)));
      for (const event of byPoint.get(entry.point)?.virtual_z || []) {
        const time = event.time_s + offset;
        // Include endpoints so a terminal recovery VZ remains inspectable.
        if (time < left || time > right || time < (entry.start_s ?? -Infinity) || time > (entry.end_s ?? Infinity)) continue;
        if (!(event.channels || []).length) continue;
        count++;
        for (const output of event.channels) {
          const key = channelKey(output, output.channel);
          const groupKey = JSON.stringify([entry.ordinal, entry.point, entry.shot, key, event.time_s]);
          if (!groups.has(groupKey)) groups.set(groupKey, {entry, key, time, events: []});
          groups.get(groupKey).events.push(event);
        }
      }
    }
    const degrees = angle => Number((angle * 180 / Math.PI).toPrecision(10));
    for (const {entry, key, time, events} of groups.values()) {
      if (!traces.has(key)) traces.set(key, {
        type: "scatter", mode: "lines+markers", x: [], y: [], customdata: [],
        name: `${channels.get(key).name} · virtual Z`, uid: traceUid("virtual-z", key),
        legendgroup: key, showlegend: false, connectgaps: false, yaxis: "y4",
        line: {color: "#5b6570", width: 1.3, dash: "dot", simplify: false},
        marker: {color: "#5b6570", symbol: "triangle-down", size: []},
        meta: {kind: "virtual_z"},
        hovertemplate: "Time %{x:.9f} µs<br>Execution %{customdata[0]} · point %{customdata[1]} · shot %{customdata[2]}<br>%{customdata[3]}<extra>%{fullData.name}</extra>"
      });
      const trace = traces.get(key);
      const detail = events.map((event, index) => {
        const gate = gateDescription(event.gate_annotations);
        const frame = Number.isFinite(event.frame_before_rad) && Number.isFinite(event.frame_after_rad) ? ` · frame ${degrees(event.frame_before_rad)}° → ${degrees(event.frame_after_rad)}°` : "";
        return (gate ? gate + "<br>" : "") + `VZ ${index + 1}/${events.length} · Rz(${degrees(event.angle_rad)}°)${frame} · zero duration`;
      }).join("<br>");
      // Coincident VZs share one line with ordered hover text. The auxiliary
      // axis is presentation-only, so these lines never affect DAC scaling.
      trace.x.push(time * 1e6, time * 1e6, time * 1e6, null);
      trace.y.push(.02, .5, .98, null);
      trace.marker.size.push(0, 0, 7, 0);
      for (let i = 0; i < 3; i++) trace.customdata.push([entry.ordinal, entry.point, entry.shot, detail]);
      trace.customdata.push(null);
    }
    return {traces: [...traces.values()], count};
  }

  async function replayExecutions(payload, entries, window, options = {}) {
    // A display offset never enters the DAC decoder or phase calculation.
    const start = fraction(window.start_s), duration = fraction(window.duration_s);
    if (!Number.isFinite(Number(window.start_s)) || !Number.isFinite(Number(window.duration_s)) || Number(window.duration_s) <= 0) {
      throw new Error("Choose a finite start and a positive window length.");
    }
    const inventory = channelInventory(payload.points), jobs = [];
    const toneInventory = ddsInventory(payload.points);
    const toneTraces = emptyDDSTraces(toneInventory), toneByKey = new Map(toneTraces.map(trace => [trace.legendgroup, trace]));
    const points = new Map(payload.points.map(point => [point.index, point])), annotations = new Map();
    const colorByPulse = options.color_by !== "channel";
    const maxSamples = options.max_samples || payload.max_samples || 100000;
    let requested = 0, ddsVertices = 0;
    for (const entry of entries) {
      const point = points.get(entry.point);
      if (!point) throw new Error("Execution references an unavailable point.");
      const offset = fraction(entry.offset_fraction || entry.offset_s || 0);
      const local = {start_s: decimal(subtract(start, offset)), duration_s: decimal(duration)};
      // Clip DDS to this execution's displayed extent before adding its offset.
      // This prevents a long hold in the local control window from overlapping
      // the next shot when only gate windows are concatenated.
      const localLeft = Math.max(Number(local.start_s), Number(entry.start_s ?? -Infinity) - Number(decimal(offset)));
      const localRight = Math.min(Number(local.start_s) + Number(local.duration_s), Number(entry.end_s ?? Infinity) - Number(decimal(offset)));
      if ((point.dds || []).length && localRight > localLeft) {
        const dds = ddsTraces(point.dds, {start_s: localLeft, duration_s: localRight - localLeft,
          offset_s: Number(decimal(offset)), entry, inventory: toneInventory});
        ddsVertices += dds.vertices;
        for (const trace of dds.traces) {
          const target = toneByKey.get(trace.legendgroup);
          for (const field of ["x", "y", "customdata"]) target[field].push(...trace[field]);
        }
      }
      for (const view of point.cards) {
        const first = Math.max(0, Math.min(view.physical_duration_samples, sampleCeil(fraction(local.start_s), view)));
        const last = Math.max(first, Math.min(view.physical_duration_samples, sampleCeil(add(fraction(local.start_s), duration), view)));
        if (last <= first) continue;
        requested += (last - first) * view.active_channels.length;
        const rate = fraction(view.sample_rate_hz);
        const clipped = {
          start_s: decimal(add(fraction(view.sample0_fraction), [BigInt(first) * rate[1], rate[0]])),
          duration_s: decimal([BigInt(last - first) * rate[1], rate[0]])
        };
        if (!annotations.has(point.index)) annotations.set(point.index, pulseAnnotations(point));
        jobs.push({entry, view, local: clipped, first, last, offset: Number(decimal(offset)), annotations: annotations.get(point.index).get(view)});
      }
    }
    // Bound the whole plotted window, not each of dozens of repeated shots.
    const overview = jobs.some(job => job.last - job.first > maxSamples) || requested > maxSamples * Math.max(1, inventory.length);
    const traces = inventory.map(item => ({
      type: "scatter", mode: options.display === "markers" && !overview ? "markers" : options.display === "both" && !overview ? "lines+markers" : "lines",
      // A null legend anchor remains clickable when zoom excludes all idle
      // samples. It is not a DAC sample and does not enter any sample count.
      x: colorByPulse ? [null] : [], y: colorByPulse ? [null] : [], customdata: colorByPulse ? [null] : [],
      name: item.name, uid: traceUid("awg", item.key), legendgroup: item.key,
      showlegend: true, connectgaps: false,
      line: {color: colorByPulse ? IDLE_COLOR : item.color, width: overview ? 1 : 1.3, simplify: false, shape: options.display === "hold" && !overview ? "hv" : "linear"},
      marker: {color: colorByPulse ? IDLE_COLOR : item.color, size: 3},
      meta: {kind: colorByPulse ? "idle" : "channel", pulse_indices: []},
      hovertemplate: "Time %{x:.9f} µs<br>Execution %{customdata[0]} · point %{customdata[1]} · shot %{customdata[2]}<br>%{customdata[3]}<extra>%{fullData.name}</extra>"
    }));
    const byKey = new Map(traces.map(trace => [trace.legendgroup, trace]));
    function traceFor(key, span) {
      const base = byKey.get(key);
      if (!colorByPulse || span.kind === "idle") return base;
      const spanKey = JSON.stringify([key, span.kind, span.pulse_indices]);
      if (!byKey.has(spanKey)) {
        const color = pulseColor(span);
        const trace = {...base, uid: traceUid("awg-span", spanKey), showlegend: false, x: [], y: [], customdata: [],
          line: {...base.line, color}, marker: {...base.marker, color},
          meta: {kind: span.kind, pulse_indices: span.pulse_indices}};
        byKey.set(spanKey, trace); traces.push(trace);
      }
      return byKey.get(spanKey);
    }
    function breakLine(trace) {trace.x.push(null); trace.y.push(null); trace.customdata.push(null);}
    let bins = 0, exact = 0, firstTable = null;
    for (const job of jobs) for (const channel of job.view.active_channels) {
      if (options.cancelled?.()) return null;
      const {entry, view, local, offset} = job;
      const key = channelKey(view, channel);
      const spans = pulseSpans(job.annotations.get(channel) || [], job.first, job.last);
      const budget = Math.max(1, Math.min(1600, Math.ceil(1600 * (job.last - job.first) / view.sample_rate_hz / Number(window.duration_s))));
      const result = await (overview ? overviewWindow : decodeWindow)(view, payload.packed, {...local, channel,
        max_samples: maxSamples, max_bins: budget, boundaries: spans.map(span => span.first)});
      const scale = options.units === "codes" ? 1 : options.units === "voltage" ? view.output_range_mv / (1000 * 32767) : 1 / 32767;
      if (result.overview) {
        bins += result.starts.length;
        let spanIndex = 0;
        for (let i = 0; i < result.starts.length; i++) {
          while (spans[spanIndex].last <= result.starts[i]) spanIndex++;
          const span = spans[spanIndex], trace = traceFor(key, span);
          const left = (view.sample0_time_s + result.starts[i] / view.sample_rate_hz + offset) * 1e6;
          const right = (view.sample0_time_s + result.stops[i] / view.sample_rate_hz + offset) * 1e6;
          trace.x.push(left, left, right, right, null);
          trace.y.push(result.minimum[i] * scale, result.maximum[i] * scale, result.maximum[i] * scale, result.minimum[i] * scale, null);
          const detail = `${pulseDescription(span, view)}<br>Samples [${result.starts[i]}, ${result.stops[i]}) · exact DAC min ${result.minimum[i]} / max ${result.maximum[i]}<br>Overview bin; zoom for individual samples`;
          const owner = {kind: span.kind, pulse_indices: span.pulse_indices, boundary: false};
          for (let j = 0; j < 4; j++) trace.customdata.push([entry.ordinal, entry.point, entry.shot, detail, result.starts[i], result.minimum[i], owner, result.stops[i]]);
          trace.customdata.push(null);
        }
      } else {
        exact += result.codes.length;
        for (const span of spans) {
          const trace = traceFor(key, span), description = pulseDescription(span, view);
          const append = (index, boundary = false) => {
            const i = index - result.first;
            trace.x.push((result.times[i] + offset) * 1e6);
            trace.y.push(result.codes[i] * scale);
            trace.customdata.push([entry.ordinal, entry.point, entry.shot,
              `${description}<br>${boundary ? "Boundary endpoint · " : ""}Card sample ${result.indices[i]} · DAC ${result.codes[i]} · nominal ${(result.codes[i] / 32767 * view.output_range_mv / 1000).toFixed(8)} V<br>Segment ${result.segmentIds[i]} · step ${result.stepIds[i]} · local T0 + ${(result.times[i] * 1e6).toFixed(9)} µs`, result.indices[i], result.codes[i],
              {kind: span.kind, pulse_indices: span.pulse_indices, boundary}]);
          };
          for (let index = span.first; index < span.last; index++) append(index);
          // Share the next REAL endpoint to preserve the original connected
          // samples / hold guide up to the boundary. Never invent a zero.
          if (colorByPulse) {
            if (span.last < result.last && options.display !== "markers") append(span.last, true);
            breakLine(trace);
          }
        }
        if (!firstTable) firstTable = {result, view, channel, entry};
      }
      // Never draw a line between independent regions or shots.
      if (!colorByPulse) breakLine(byKey.get(key));
    }
    const virtual = virtualZTraces(payload.points, entries, window, inventory);
    return {traces: [...traces, ...virtual.traces, ...toneTraces], inventory, overview, bins, exact, requested, firstTable, virtualZCount: virtual.count,
      ddsInventory: toneInventory, ddsVertices, hasDDS: toneInventory.length > 0};
  }

  const api = {decodeWindow, overviewWindow, sequenceWindow, walk, sampleCeil, fraction, decimal, inputWindow,
    formatWindow, queryWindow, pulseWindow, previewWindow, windowSummary,
    channelInventory, replayExecutions, ddsWindow, ddsTraces, ddsInventory, outputAxes, toggleChannel, traceUid,
    cacheLimitBytes: CACHE_LIMIT};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TaiyiOutputSimulator = api;
  if (typeof document === "undefined" || !document.getElementById("output-data")) return;

  const payload = JSON.parse(document.getElementById("output-data").textContent);
  const $ = id => document.getElementById(id);
  const controls = ["point", "group", "card", "channel", "pulse", "start", "duration", "units", "display", "color-by", "selection-mode", "projection", "execution-start", "execution-count", "shot-index"];
  const elements = Object.fromEntries(controls.map(id => [id, $(id)]));
  const query = new URLSearchParams(root.location.search);
  const hiddenChannels = new Set();
  let current = null, generation = 0, selectedPulse = null, plotEventsBound = false;
  let plotQueue = Promise.resolve();
  function queuePlot(callback) {
    const queued = plotQueue.then(callback);
    plotQueue = queued.catch(() => {});
    return queued;
  }
  function notice(message, kind = "") {
    $("notice").textContent = message;
    $("notice").className = kind;
  }
  function fill(element, values, selected) {
    element.replaceChildren();
    for (const [value, label] of values) {
      const option = document.createElement("option");
      option.value = value; option.textContent = label; element.append(option);
    }
    if (selected !== null && selected !== undefined && values.some(item => String(item[0]) === String(selected))) element.value = String(selected);
  }
  function point() { return payload.points[Number(elements.point.value)]; }
  function allViews() { return [...point().cards, ...(point().dds || [])]; }
  function views() { return allViews().filter(view => view.group === elements.group.value); }
  function view() { return views().find(item => item.card === elements.card.value); }
  function pulses() { return view() ? (view().pulses || []).filter(pulse => pulse.channel === Number(elements.channel.value)) : []; }
  function updateGroups(preferred) {
    fill(elements.group, [...new Set(allViews().map(item => item.group))].map(item => [item, item]), preferred);
  }
  function updateCards(preferred) {
    fill(elements.card, [...new Set(views().map(item => item.card))].map(item => [item, item]), preferred);
  }
  function updateChannels(preferred) {
    fill(elements.channel, (view() ? view().active_channels : []).map(item => [item, `CH ${item}`]), preferred);
  }
  function updatePulses(logicalId) {
    const items = pulses();
    const numbered = view() ? pulseAnnotations(point()).get(view())?.get(Number(elements.channel.value)) || [] : [];
    fill(elements.pulse, [["", view()?.mode === "dds" ? "DDS tones · no AWG pulse" : "Choose a pulse for inspection"], ...items.map((pulse, i) => {
      const start = view().sample0_time_s + pulse.start_sample / view().sample_rate_hz;
      const ordinal = numbered.find(item => item.pulse === pulse)?.index ?? i;
      return [i, `Pulse ${ordinal + 1}${pulse.logical_id?.length ? ` · ${pulse.logical_id.join(", ")}` : ""} · ${(start * 1e6).toPrecision(7)} µs`];
    })], "");
    if (logicalId) {
      const index = items.findIndex(pulse => (pulse.logical_id || []).includes(logicalId));
      if (index >= 0) elements.pulse.value = String(index);
    }
    showMetadata();
  }
  function showMetadata() {
    selectedPulse = elements.pulse.value === "" ? null : pulses()[Number(elements.pulse.value)];
    const selected = view(), items = pulses();
    $("focus-pulse").disabled = !items.length;
    $("pulse-edge").disabled = !items.length;
    if (!selected) {
      $("metadata").textContent = "No Spectrum program in this point.";
      $("intervals").textContent = "No scheduled Spectrum intervals in this point.";
      return;
    }
    if (selected.mode === "dds") {
      const curves = selected.curves.filter(curve => curve.channel === Number(elements.channel.value));
      $("intervals").textContent = `Inspection only · point ${point().index} · ${selected.card} / CH ${elements.channel.value}: DDS cores ${curves.map(curve => curve.core).join(", ")}. These selectors do not hide other channels.`;
      $("metadata").textContent = `DDS command prediction; declared trigger latency ${selected.trigger_latency_ns} ns.\n` +
        `Command frames: ${(selected.command_start_s * 1e6).toFixed(6)}–${(selected.command_end_s * 1e6).toFixed(6)} µs from local T0.\n` +
        `Frequency / amplitude ramps and nonzero hold; no DAC codes or absolute RF phase.\n` +
        `Hold continues while the session remains open. Unknown host / shot intervals are not assigned durations.`;
      return;
    }
    const intervals = items.slice(0, 12).map(pulse => {
      const window = pulseWindow(selected, pulse), end = decimal(add(fraction(window.start_s), fraction(window.duration_s)));
      return `${formatWindow(window.start_s, 0).start_us}–${formatWindow(end, 0).start_us} µs`;
    });
    $("intervals").textContent = `Inspection only · point ${point().index} · ${selected.card} / CH ${elements.channel.value}: ${intervals.join("; ") || "no scheduled pulse"}. These selectors do not hide other channels.`;
    const lines = [`Calibration: ${selected.calibration_status}; declared latency: ${selected.trigger_latency_samples} samples.`];
    if (selectedPulse) {
      lines.push(`Carrier ${selectedPulse.frequency_hz} Hz; resolved start phase ${selectedPulse.phase_rad.toPrecision(8)} rad.`);
      lines.push(`Envelope: ${selectedPulse.envelope.kind}; amplitude ${selectedPulse.amplitude}.`);
      if (selectedPulse.phase_modulation) lines.push(`Local phase: ${JSON.stringify(selectedPulse.phase_modulation)}.`);
    }
    $("metadata").textContent = lines.join("\n");
  }
  function selection() {
    const mode = elements["selection-mode"].value;
    const selected = root.ViewerExecution.selectExecutions(payload.execution_axis, {
      start: mode === "detail" ? 0 : Math.max(0, Math.trunc(Number(elements["execution-start"].value) || 0)),
      count: Math.max(1, Math.min(500, Math.trunc(Number(elements["execution-count"].value) || 50))),
      point: mode === "all" ? null : point().index,
      shot: mode === "detail" ? Math.max(0, Math.min(payload.execution_axis.shots - 1, Math.trunc(Number(elements["shot-index"].value) || 0))) : null,
      projection: elements.projection.value
    });
    if (mode === "detail") selected.entries = selected.entries.map(entry => ({...entry,
      offset_s: 0, offset_fraction: ["0", "1"], start_s: entry.local_start_s, end_s: entry.local_end_s}));
    return {...selected, local: mode === "detail"};
  }
  function setWindow(start, duration) {
    const formatted = formatWindow(start, duration);
    elements.start.value = formatted.start_us; elements.duration.value = formatted.duration_us;
  }
  function fitSelection() {
    const selected = selection(), entries = selected.entries;
    const first = entries.length ? entries[0].start_s : 0;
    const last = entries.length ? entries[entries.length - 1].end_s : first + 1e-6;
    setWindow(first, Math.max(1e-12, last - first));
  }
  function selectPreview(edge = false) {
    if (elements.pulse.value === "" && pulses().length) elements.pulse.value = "0";
    showMetadata();
    if (!selectedPulse) return false;
    elements["selection-mode"].value = "detail";
    const window = previewWindow(view(), selectedPulse, payload.max_samples, edge);
    setWindow(window.start_s, window.duration_s);
    return true;
  }
  function sampleTable(data) {
    $("samples").replaceChildren();
    if (!data.firstTable) {
      $("sample-note").textContent = data.overview ? "Overview bins are not individual sample rows. Zoom to inspect exact DAC codes." : "No finite-program samples in this window.";
      return;
    }
    const {result, view: selected, channel, entry} = data.firstTable;
    const count = Math.min(result.codes.length, 200), fragment = document.createDocumentFragment();
    for (let index = 0; index < count; index++) {
      const row = document.createElement("tr");
      for (const value of [result.indices[index], (result.times[index] * 1e9).toFixed(4), result.codes[index], (result.codes[index] / 32767 * selected.output_range_mv / 1000).toFixed(8)]) {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      }
      fragment.append(row);
    }
    $("samples").append(fragment);
    $("sample-note").textContent = `First exact trace in this window: execution ${entry.ordinal}, point ${entry.point}, shot ${entry.shot}, ${selected.card} / CH ${channel}. Showing ${count} of ${result.codes.length} samples; table times remain local T0. Hover any plotted channel for its exact codes.`;
  }
  function boundaries(selected, start, end) {
    return selected.entries.filter(entry => entry.start_s >= start && entry.start_s <= end).map(entry => ({
      type: "line", xref: "x", yref: "paper", x0: entry.start_s * 1e6, x1: entry.start_s * 1e6, y0: 0, y1: 1,
      line: {color: "#9ca69c", width: 1, dash: "dot"}
    }));
  }
  async function render() {
    const token = ++generation;
    notice("Reading final DAC samples and native DDS commands…");
    try {
      const selected = selection(), window = inputWindow(elements.start.value, elements.duration.value);
      const start = Number(window.start_s), duration = Number(window.duration_s);
      const data = await replayExecutions(payload, selected.entries, window, {
        units: elements.units.value, display: elements.display.value, color_by: elements["color-by"].value, cancelled: () => token !== generation
      });
      if (!data || token !== generation) return;
      await queuePlot(async () => {
        if (token !== generation) return;
        const empty = !data.requested && !data.ddsVertices && !data.virtualZCount;
        for (const trace of data.traces) trace.visible = hiddenChannels.has(trace.legendgroup || trace.uid) ? "legendonly" : true;
        const visibleEntries = selected.entries.filter(entry => entry.end_s >= start && entry.start_s <= start + duration);
        const annotations = visibleEntries.length <= 16 ? visibleEntries.map(entry => ({
          xref: "x", yref: "paper", x: (Math.max(start, entry.start_s) + Math.min(start + duration, entry.end_s)) * 5e5,
          y: 1.04, text: `P${entry.point} · S${entry.shot}`, showarrow: false, font: {size: 11, color: "#68716c"}
        })) : [];
        const layout = {
          paper_bgcolor: "#fffef9", plot_bgcolor: "#fffef9", font: {family: "Segoe UI, Arial, sans-serif", color: "#252b2a"},
          height: data.hasDDS ? 850 : 560,
          margin: {l: 90, r: 24, t: 42, b: data.hasDDS ? 150 : 92}, showlegend: true, hovermode: "closest", dragmode: "zoom",
          legend: {orientation: "h", y: data.hasDDS ? -.16 : -.2, x: 0, itemdoubleclick: false, title: {text: "Click a channel / DDS quantity to show or hide"}},
          xaxis: {anchor: "free", position: 0, title: {text: selected.local ? "Local control T0 (µs)" : elements.projection.value === "awg" ? "Accumulated Spectrum gate windows (µs) · omitted stages at boundaries" : "Accumulated known control windows (µs)"},
            range: [start * 1e6, (start + duration) * 1e6], gridcolor: "#e2e5dc", zerolinecolor: "#939b92"},
          ...outputAxes(elements.units.value, data.hasDDS, data.inventory.length > 0),
          shapes: selected.local ? [] : boundaries(selected, start, start + duration),
          annotations: empty ? [{xref: "paper", yref: "paper", x: .5, y: .6, text: "No AWG samples or DDS commands in this window", showarrow: false}] : annotations
        };
        if ($("waveform").style) $("waveform").style.height = `${layout.height}px`;
        await Plotly.react("waveform", data.traces, layout, {responsive: true, displaylogo: false, scrollZoom: true,
          modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"], toImageButtonOptions: {filename: "taiyidsl_dac_sequence", format: "svg"}});
        if (token !== generation) return;
        current = {data, start, duration};
        if (!plotEventsBound) {
          $("waveform").on("plotly_relayout", onRelayout);
          $("waveform").on("plotly_legendclick", event => {
            const trace = current?.data.traces[event.curveNumber];
            if (!trace) return false;
            const {visible, indices} = toggleChannel(current.data.traces, hiddenChannels, trace);
            Plotly.restyle("waveform", {visible}, indices);
            return false;
          });
          plotEventsBound = true;
        }
        $("trace-title").textContent = (data.hasDDS ? `${data.inventory.length} AWG channels + ${data.ddsInventory.length} DDS tones` : `All ${data.inventory.length} card channels`) + ` · ${selected.entries.length} execution windows`;
        $("stats").textContent = (data.overview ? `${data.requested.toLocaleString()} samples covered · ${data.bins.toLocaleString()} min/max bins` : `${data.exact.toLocaleString()} exact samples · no skipped samples`) +
          (data.virtualZCount ? ` · ${data.virtualZCount} virtual Z updates` : "") +
          (data.hasDDS ? ` · ${data.ddsVertices.toLocaleString()} DDS command vertices` : "");
        sampleTable(data);
        $("execution-summary").textContent = selected.local ? `Point ${selected.entries[0]?.point} · shot ${selected.entries[0]?.shot} · execution ${selected.entries[0]?.ordinal} · local time` :
          `Execution order ${payload.execution_axis.mode} · ${selected.entries.length} of ${selected.filtered_total} selected executions${selected.truncated ? " · more available in Advanced" : ""}`;
        $("page-next").hidden = !selected.truncated;
        $("page-previous").hidden = Number(elements["execution-start"].value) <= 0 || selected.local;
        const scope = selected.local ? "Local shot/control time; all channels remain visible." : "Consecutive compiled windows. Host waits and variable native preparation are omitted at the dotted boundaries; this is not measured wall-clock time.";
        notice(`${scope} ${data.overview ? "Exact min/max of final DAC samples; zoom for individual samples." : "Final packed DAC samples; display offsets do not change carrier phase."}` +
          (data.hasDDS ? " DDS frequency/amplitude are quantized command predictions, not RF/DAC samples. Tone output holds through omitted shot/host gaps while the session remains open; their durations are unknown." : "") +
          (empty ? data.hasDDS ? " No Spectrum output in this selected window." : " This scan point contains no Spectrum AWG program in the selected window." : ""));
      });
    } catch (error) { if (token === generation) notice(error.message, "error"); }
  }
  function changeSelection() { fitSelection(); render(); }
  function onRelayout(event) {
    if (!current) return;
    if (event["xaxis.autorange"]) {fitSelection(); render(); return;}
    const range = event["xaxis.range"] || [event["xaxis.range[0]"], event["xaxis.range[1]"]];
    if (!range.every(Number.isFinite) || range[1] <= range[0]) return;
    const window = queryWindow(inputWindow(range[0], 0).start_s, inputWindow(range[1], 0).start_s);
    const start = Number(window.start_s), duration = Number(window.duration_s);
    setWindow(window.start_s, window.duration_s);
    if (start !== current.start || duration !== current.duration) render();
  }

  $("experiment").textContent = payload.name;
  fill(elements.point, payload.points.map((item, i) => [i, `${item.index} · ${JSON.stringify(item.coordinates)}`]), query.get("point"));
  const detailQuery = ["point", "shot", "group", "card", "channel", "logical_id", "t0", "t1"].some(key => query.has(key));
  fill(elements["selection-mode"], [["all", "All points and shots"], ["point", "Selected point's shots"], ["detail", "One point / shot · local time"]], detailQuery ? "detail" : "all");
  fill(elements["color-by"], [["pulse", "By physical pulse"], ["channel", "By channel"]], query.get("color_by") === "channel" ? "channel" : "pulse");
  fill(elements.projection, [["awg", "Spectrum gate windows · omit other stages"], ["control", "Full known control windows"]], "awg");
  elements["execution-start"].value = "0"; elements["execution-count"].value = "50";
  elements["shot-index"].value = query.get("shot") || "0";
  updateGroups(query.get("group")); updateCards(query.get("card")); updateChannels(query.get("channel")); updatePulses(query.get("logical_id"));
  const t0 = query.get("t0"), t1 = query.get("t1");
  if (t0 !== null && t1 !== null && Number.isFinite(Number(t0)) && Number.isFinite(Number(t1)) && greater(fraction(t1), fraction(t0))) {
    const window = queryWindow(t0, t1); setWindow(window.start_s, window.duration_s);
  } else if (selectedPulse) selectPreview();
  else fitSelection();

  elements.point.addEventListener("change", () => {
    updateGroups(elements.group.value); updateCards(elements.card.value); updateChannels(elements.channel.value); updatePulses();
    if (elements["selection-mode"].value !== "all") changeSelection();
  });
  elements.group.addEventListener("change", () => {updateCards(); updateChannels(); updatePulses();});
  elements.card.addEventListener("change", () => {updateChannels(); updatePulses();});
  elements.channel.addEventListener("change", () => updatePulses());
  elements.pulse.addEventListener("change", showMetadata);
  for (const id of ["selection-mode", "projection", "execution-start", "execution-count", "shot-index"]) elements[id].addEventListener("change", changeSelection);
  $("full-sequence").addEventListener("click", changeSelection);
  $("focus-pulse").addEventListener("click", () => {if (selectPreview()) render();});
  $("pulse-edge").addEventListener("click", () => {if (selectPreview(true)) render();});
  elements.units.addEventListener("change", render); elements.display.addEventListener("change", render);
  elements["color-by"].addEventListener("change", render);
  $("render").addEventListener("click", render);
  for (const id of ["start", "duration"]) elements[id].addEventListener("keydown", event => {if (event.key === "Enter") render();});
  $("page-next").addEventListener("click", () => {elements["execution-start"].value = selection().next_start; changeSelection();});
  $("page-previous").addEventListener("click", () => {elements["execution-start"].value = Math.max(0, Number(elements["execution-start"].value) - Number(elements["execution-count"].value)); changeSelection();});
  render();
})(typeof globalThis !== "undefined" ? globalThis : this);
