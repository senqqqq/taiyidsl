/* Concatenated known-window coordinates. Never a measured wall-clock axis. */
(function (root) {
  "use strict";
  const prepared = new WeakMap(), ZERO = [0n, 1n];
  const MAX_PAGE_SIZE = 500;
  function gcd(a, b) {
    a = a < 0n ? -a : a;
    while (b) { const next = a % b; a = b; b = next; }
    return a || 1n;
  }
  function rational(value) {
    const n = BigInt(value[0]), d = BigInt(value[1]);
    if (d <= 0n) throw new Error("Invalid execution-axis fraction.");
    const common = gcd(n, d);
    return [n / common, d / common];
  }
  function add(a, b) { return rational([a[0] * b[1] + b[0] * a[1], a[1] * b[1]]); }
  function negate(a) { return [-a[0], a[1]]; }
  function scale(a, count) { return rational([a[0] * BigInt(count), a[1]]); }
  function number(a) { return Number(a[0]) / Number(a[1]); }
  function pair(a) { return a.map(String); }
  function integer(value, label, minimum = 0) {
    if (!Number.isSafeInteger(value) || value < minimum) throw new Error("Invalid " + label + ".");
    return value;
  }
  function prepare(axis, projection) {
    let cache = prepared.get(axis);
    if (!cache) { cache = {}; prepared.set(axis, cache); }
    if (cache[projection]) return cache[projection];
    if (!['control', 'awg'].includes(projection)) throw new Error("Unknown time projection.");
    const shots = integer(axis.shots, "shot count", 1);
    const points = new Map(axis.points.map(p => [p.index, p]));
    const sequence = axis.scope === 'chunk' ? axis.chunks : [{points: axis.points.map(p => p.index)}];
    let ordinal = 0, elapsed = ZERO;
    const groups = [], locations = new Map();
    for (const batch of sequence) {
      const prefixes = [ZERO], starts = [], durations = [];
      for (const id of batch.points) {
        const p = points.get(id);
        if (!p) throw new Error("Execution plan references a missing point.");
        const window = p[projection];
        const start = rational(window.start_fraction), end = rational(window.end_fraction);
        const duration = add(end, negate(start));
        if (duration[0] < 0n) throw new Error("Negative execution window.");
        starts.push(start); durations.push(duration);
        prefixes.push(add(prefixes[prefixes.length - 1], duration));
      }
      const group = {points: batch.points, ordinal, elapsed, prefixes, starts, durations};
      batch.points.forEach((id, local) => locations.set(id, {group, local}));
      groups.push(group);
      ordinal += batch.points.length * shots;
      elapsed = add(elapsed, scale(prefixes[prefixes.length - 1], shots));
    }
    integer(ordinal, "execution count");
    if (ordinal !== axis.total_executions) throw new Error("Execution count disagrees with plan.");
    cache[projection] = {shots, points, groups, locations, elapsed, total: ordinal};
    return cache[projection];
  }
  function ordinalFor(data, mode, point, shot) {
    const {group, local} = data.locations.get(point);
    return group.ordinal + (mode === 'cyclic' ? shot * group.points.length + local : local * data.shots + shot);
  }
  function entryAt(axis, data, ordinal) {
    let low = 0, high = data.groups.length - 1;
    while (low < high) {
      const middle = Math.ceil((low + high) / 2);
      if (data.groups[middle].ordinal <= ordinal) low = middle;
      else high = middle - 1;
    }
    const group = data.groups[low], index = ordinal - group.ordinal;
    const cyclic = axis.mode === 'cyclic', count = group.points.length;
    const local = cyclic ? index % count : Math.floor(index / data.shots);
    const shot = cyclic ? Math.floor(index / count) : index % data.shots;
    const point = group.points[local], metadata = data.points.get(point);
    const within = cyclic ? add(scale(group.prefixes[count], shot), group.prefixes[local]) :
      add(scale(group.prefixes[local], data.shots), scale(group.durations[local], shot));
    const start = add(group.elapsed, within), end = add(start, group.durations[local]);
    const offset = add(start, negate(group.starts[local]));
    return {ordinal, point, shot, chunk: metadata.layout.chunk,
      offset_s: number(offset), offset_fraction: pair(offset),
      local_start_s: number(group.starts[local]),
      local_end_s: number(add(group.starts[local], group.durations[local])),
      start_s: number(start), end_s: number(end),
      start_fraction: pair(start), end_fraction: pair(end),
      unknown_boundary: true,
      native_prefix_duration_unknown: metadata.native_prefix_duration_unknown};
  }
  function selectExecutions(axis, options = {}) {
    const {start = 0, count = 50, point = null, shot = null, projection = 'control'} = options;
    integer(start, "first execution"); integer(count, "execution window count", 1);
    if (count > MAX_PAGE_SIZE) throw new Error("At most " + MAX_PAGE_SIZE + " executions can be displayed per window.");
    const data = prepare(axis, projection), ordinals = [];
    if (point !== null && !data.points.has(point)) throw new Error("Unknown execution point.");
    if (shot !== null && (integer(shot, "shot") >= data.shots)) throw new Error("Shot is outside the experiment.");
    let available = 0, matching = data.total;
    if (point !== null && shot !== null) {
      const value = ordinalFor(data, axis.mode, point, shot);
      matching = 1; available = Number(value >= start);
      if (available) ordinals.push(value);
    } else if (point !== null) {
      const first = ordinalFor(data, axis.mode, point, 0);
      const stride = axis.mode === 'cyclic' ? data.locations.get(point).group.points.length : 1;
      const begin = Math.max(0, Math.ceil((start - first) / stride));
      matching = data.shots; available = Math.max(0, data.shots - begin);
      for (let index = begin; index < Math.min(data.shots, begin + count); index++) ordinals.push(first + index * stride);
    } else if (shot !== null) {
      const selected = [...data.points.keys()].map(p => ordinalFor(data, axis.mode, p, shot)).filter(value => value >= start).sort((a, b) => a - b);
      matching = data.points.size; available = selected.length;
      ordinals.push(...selected.slice(0, count));
    } else {
      available = Math.max(0, data.total - start);
      for (let index = start; index < Math.min(data.total, start + count); index++) ordinals.push(index);
    }
    const entries = ordinals.map(ordinal => entryAt(axis, data, ordinal));
    return {entries, total: data.total, filtered_total: matching,
      truncated: available > entries.length, start, count,
      next_start: entries.length ? entries[entries.length - 1].ordinal + 1 : start,
      duration_s: number(data.elapsed), projection,
      axis_kind: 'concatenated_known_windows', wall_clock_time: false};
  }
  const api = {selectExecutions, MAX_PAGE_SIZE};
  root.ViewerExecution = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
