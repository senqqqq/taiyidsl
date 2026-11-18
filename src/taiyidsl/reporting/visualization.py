"""Self-contained pulse sheets for inspecting compiled logical and physical time."""

from __future__ import annotations

import json
from decimal import Decimal, localcontext
from fractions import Fraction
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from ..serialization import to_jsonable as _jsonable
from .resources import asset_text

_COLORS = {
    "output": "#3562a6",
    "awg": "#087f82",
    "trigger": "#627944",
    "frame": "#b97819",
    "wait": "#a5aaa8",
    "control": "#867793",
    "structure": "#b4cecb",
}
_FRAME_KINDS = {"rz", "shift_phase", "reset_frame", "reset_phase"}


def _safe_json(value):
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _hover_text(value):
    # Plotly's SVG text decoder supports lt/gt/amp and numeric entities, but not
    # quot/apos. Quotes in text nodes are safe and should remain ordinary text.
    # Escape percent as well so user text cannot introduce %{...} interpolation.
    return escape(json.dumps(value, ensure_ascii=False), quote=False).replace(
        "%", "&#37;"
    )


def _trace(event, tick_seconds, *, lane=None, role=None, driver_tick=None):
    """Retain endpoint coordinates; the renderer turns intervals into rectangles.

    Keeping the endpoint representation also makes timing inspection independent
    of Plotly's bar/base representation. All displayed coordinates are microseconds.
    Driver parameter mu values remain untouched and carry their own time unit.
    """
    start = float(event.start_mu) * tick_seconds * 1e6
    duration = float(event.duration_mu) * tick_seconds * 1e6
    params = _jsonable(event.params)
    awg = event.kind.startswith("spectrum_") or params.get("route") == "spectrum"
    if role is None:
        role = (
            "frame"
            if event.kind in _FRAME_KINDS
            else "wait"
            if event.kind
            in {
                "delay",
                "scope_delay",
                "native_delay",
                "reserve",
                "spectrum_idle",
                "spectrum_zero_tail",
            }
            else "structure"
            if event.kind == "spectrum_region"
            else "control"
            if event.kind == "native_timed"
            else "output"
        )
    color = _COLORS["awg" if awg and role == "output" else role]
    if event.kind in {"trig", "trigger", "ttl_output", "measure", "acquire", "prepare"}:
        color = _COLORS["trigger"]
    details = {
        "kind": event.kind,
        "signal": event.signal,
        "start_mu": event.start_mu,
        "end_mu": event.start_mu + event.duration_mu,
        "duration_mu": event.duration_mu,
        "time_unit_s": tick_seconds,
        "start_us": start,
        "end_us": start + duration,
        "duration_us": duration,
        "parameters": params,
        "source": event.source,
        "resources": list(getattr(event, "resources", ())),
        "logical_ids": list(getattr(event, "logical_ids", ())),
    }
    if driver_tick is not None:
        details["driver_parameter_time_unit_s"] = driver_tick
    hover_fields = {
        "Operation": event.kind,
        "Signal": event.signal,
        "Start (µs)": start,
        "End (µs)": start + duration,
        "Duration (µs)": duration,
    }
    for label, keys in (
        ("Frequency (Hz)", ("frequency", "frequency_hz")),
        ("Amplitude", ("amplitude",)),
        ("Phase (rad)", ("phase", "phase_rad")),
    ):
        value = next((params[key] for key in keys if key in params), None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            hover_fields[label] = value
    tooltip = "<br>".join(
        f"<b>{key}</b>: {_hover_text(value)}" for key, value in hover_fields.items()
    )
    shape = params.get("shape", params.get("envelope", {}).get("kind", ""))
    label = {
        "play": "play · AWG" if awg else "play",
        "spectrum_output": "AWG",
        "scope_delay": "branch wait",
        "native_delay": "preparation-to-control wait",
        "native_timed": f"{params.get('method', event.signal)} · native / opaque",
        "spectrum_idle": "zero output",
        "spectrum_zero_tail": "zero tail",
        "dds_output": "DDS",
        "spectrum_dds": "DDS ramp / hold",
        "move": "move",
        "ttl_output": "TTL",
        "spectrum_region": "triggered sequence",
    }.get(event.kind, event.kind)
    if shape:
        label += " · " + shape.replace("flat_top_gaussian", "flat-top Gaussian")
    waveform = params.get("waveform_link")
    return {
        "type": "scatter",
        "x": [start, start + duration],
        "y": [event.signal, event.signal],
        "mode": "markers",
        "marker": {"color": color, "size": 8},
        "name": event.kind,
        "showlegend": False,
        "hovertemplate": tooltip + "<extra></extra>",
        "meta": {
            "lane": lane or event.signal,
            "role": role,
            "label": label,
            "color": color,
            "engine": (
                "Spectrum DDS"
                if params.get("mode") == "dds" or event.kind == "move"
                else "Spectrum AWG"
                if awg
                else "ARTIQ"
            ),
            "details": details,
            "waveform": waveform,
        },
    }


def _seconds_text(value):
    """Serialize a grid timestamp without a floating microsecond round trip."""
    with localcontext() as context:
        context.prec = 50
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _spectrum_traces(event, tick_seconds, logical_by_id, origin_mu=0):
    """Expand physical RF gates using native trigger mu and card sample time."""
    result = []
    group = event.params
    trigger_unit = group["trigger_time_unit_s"]
    trigger_s = group["trigger_physical_mu"] * Fraction(str(trigger_unit))
    origin_s = origin_mu * Fraction(str(tick_seconds))
    for card_name, card in group["cards"].items():
        contract, program = card["contract"], card["program"]
        if program.get("kind") == "dds":
            first = (
                trigger_s + Fraction(str(contract["dds_trigger_latency_ns"])) / 10**9
            )
            for index, frame in enumerate(program["frames"]):
                ending = (
                    program["frames"][index + 1]["at_mu"]
                    if index + 1 < len(program["frames"])
                    else frame["at_mu"]
                )
                for tone in frame["tones"]:
                    synthetic = SimpleNamespace(
                        kind="spectrum_dds",
                        signal=f"{card_name}:core{tone['core']}",
                        start_mu=float((first - origin_s) / Fraction(str(tick_seconds)))
                        + frame["at_mu"],
                        duration_mu=ending - frame["at_mu"],
                        params={
                            **tone,
                            "mode": "dds",
                            "card": card_name,
                            "model": "command_trajectory_not_DAC_samples",
                            "terminal_hold": index == len(program["frames"]) - 1,
                        },
                        source=event.source,
                        resources=(card_name,),
                        logical_ids=(),
                    )
                    result.append(
                        _trace(
                            synthetic,
                            tick_seconds,
                            lane=f"DDS {card_name} · ch{tone['channel']} "
                            f"· tone {tone['core']}",
                        )
                    )
            continue
        rate = Fraction(str(contract["sample_rate_hz"]))
        first_sample_s = trigger_s + contract["trigger_latency_samples"] / rate
        channels_with_pulses = set()
        for pulse in program["pulses"]:
            channel = pulse["channel"]
            channels_with_pulses.add(channel)
            ids = pulse.get("logical_id", ())
            if isinstance(ids, str):
                ids = [ids]
            logical = next((logical_by_id[i] for i in ids if i in logical_by_id), None)
            params = dict(pulse)
            if logical is not None:
                params.update(
                    {
                        key: logical.params[key]
                        for key in (
                            "frame",
                            "frame_phase",
                            "virtual_z_phase",
                            "requested_phase",
                            "qubit",
                        )
                        if key in logical.params
                    }
                )
            params.update(
                route="spectrum",
                frequency=pulse["frequency_hz"],
                phase=pulse["phase_rad"],
                group=group["name"],
                phase_reference=group["phase_reference"],
                card=card_name,
                channel=channel,
                waveform_link={
                    "group": group["name"],
                    "card": card_name,
                    "channel": channel,
                    "logical_id": next(iter(ids), ""),
                    "t0": _seconds_text(
                        first_sample_s + pulse["start_sample"] / rate - origin_s
                    ),
                    "t1": _seconds_text(
                        first_sample_s
                        + (pulse["start_sample"] + pulse["duration_samples"]) / rate
                        - origin_s
                    ),
                },
            )
            synthetic = SimpleNamespace(
                kind="spectrum_output",
                signal=f"{card_name}:ch{channel}",
                start_mu=(first_sample_s + pulse["start_sample"] / rate) / tick_seconds,
                duration_mu=pulse["duration_samples"] / rate / tick_seconds,
                params=params,
                source=pulse.get("source", event.source),
                resources=(card_name,),
                logical_ids=ids,
            )
            result.append(
                _trace(synthetic, tick_seconds, lane=f"AWG {card_name} · ch{channel}")
            )
        for channel in contract["active_channels"]:
            empty = channel not in channels_with_pulses
            begin = 0 if empty else program["logical_duration_samples"]
            end = program["physical_duration_samples"]
            if end <= begin:
                continue
            synthetic = SimpleNamespace(
                kind="spectrum_idle" if empty else "spectrum_zero_tail",
                signal=f"{card_name}:ch{channel}",
                start_mu=(first_sample_s + begin / rate) / tick_seconds,
                duration_mu=(end - begin) / rate / tick_seconds,
                params={
                    "route": "spectrum",
                    "amplitude": 0,
                    "card": card_name,
                    "channel": channel,
                },
                source=event.source,
                resources=(card_name,),
                logical_ids=(),
            )
            result.append(
                _trace(synthetic, tick_seconds, lane=f"AWG {card_name} · ch{channel}")
            )
    summary = SimpleNamespace(
        kind="spectrum_program",
        signal=event.signal,
        start_mu=trigger_s / tick_seconds,
        duration_mu=0,
        params={
            key: value
            for key, value in group.items()
            if key not in {"cards", "calibration"}
        },
        source=event.source,
        resources=event.resources,
        logical_ids=event.logical_ids,
    )
    result.append(
        _trace(
            summary,
            tick_seconds,
            lane=f"{event.signal} · program",
            role="control",
            driver_tick=trigger_unit,
        )
    )
    return result


def _physical_traces(
    events, tick_seconds, *, driver_tick=None, logical_events=(), origin_mu=0
):
    """Reconstruct output blocks at effect boundaries, alongside driver events."""
    opened, configuration, spans = {}, {}, []
    logical_by_id = {
        key: event for event in logical_events for key in event.logical_ids
    }

    def close_span(start, end, params, kind):
        if end <= start.start_mu:
            return
        interval = SimpleNamespace(
            kind=kind,
            signal=start.signal,
            start_mu=start.start_mu,
            duration_mu=end - start.start_mu,
            params=params,
            source=start.source,
            resources=getattr(start, "resources", ()),
            logical_ids=getattr(start, "logical_ids", ()),
        )
        spans.append(_trace(interval, tick_seconds, driver_tick=driver_tick))

    for event in sorted(events, key=lambda item: item.start_mu):
        if event.kind in {"dds_effect", "dds_ram_effect", "dds_polar_effect"}:
            params = dict(event.params)
            if event.signal in opened:
                start, previous = opened[event.signal]
                close_span(start, event.start_mu, previous, "dds_output")
                opened[event.signal] = (event, params)
            configuration[event.signal] = params
        elif event.kind in {"dds_config", "dds_ram_start"}:
            if event.params.get("ram_boundary"):
                continue
            configuration[event.signal] = dict(event.params)
            if event.kind == "dds_ram_start" and event.signal in opened:
                start, _ = opened[event.signal]
                opened[event.signal] = (start, dict(event.params))
        elif (
            event.kind in {"ttl_on", "ttl_off"} and "digital_idle_level" in event.params
        ):
            level = int(event.kind == "ttl_on")
            if level != event.params["digital_idle_level"]:
                opened[event.signal] = (event, dict(event.params))
            elif event.signal in opened:
                start, params = opened.pop(event.signal)
                close_span(start, event.start_mu, params, "ttl_output")
        elif event.kind in {"dds_on", "ttl_on"}:
            opened[event.signal] = (event, dict(configuration.get(event.signal, {})))
        elif event.kind in {"dds_off", "ttl_off"} and event.signal in opened:
            start, params = opened.pop(event.signal)
            close_span(start, event.start_mu, params, event.kind[:3] + "_output")
    for event in events:
        if event.kind == "spectrum_program":
            spans.extend(
                _spectrum_traces(event, tick_seconds, logical_by_id, origin_mu)
            )
        else:
            spans.append(
                _trace(
                    event,
                    tick_seconds,
                    lane=f"{event.signal} · control",
                    role="control",
                    driver_tick=driver_tick,
                )
            )
    return spans


def _native_stage_trace(call, tick, *, origin_us=0):
    """Show declared duration, never pretend to reconstruct native outputs."""
    start = call["start_s_from_control_T0"]
    duration = call["end_s_from_control_T0"] - start
    owner = f"{call['component']}." if "component" in call else ""
    return _trace(
        SimpleNamespace(
            kind="native_timed",
            signal=owner + call["method"],
            start_mu=(start + origin_us / 1e6) / tick,
            duration_mu=duration / tick,
            params={**call, "native_output": "opaque_not_simulated"},
            source="Declared native lifecycle; waveform not simulated",
        ),
        tick,
        lane="Native ARTIQ · declared timing",
        role="structure",
    )


def _payload(compiled):
    from ..host_plan import execution_plan_for, point_execution_layout
    from .native_view import native_lifecycle

    lifecycle = native_lifecycle(compiled)
    execution = point_execution_layout(execution_plan_for(compiled))
    tick = float(compiled.setup.tick_seconds)
    spectrum = getattr(compiled.setup, "spectrum", None)
    driver_tick = spectrum.artiq_tick_seconds if spectrum is not None else tick
    result = []
    for point in compiled.points:
        physical = _physical_traces(
            point.physical_events,
            tick,
            driver_tick=driver_tick,
            logical_events=point.logical_events,
            origin_mu=point.origin_mu,
        )
        physical_by_id = {}
        for trace in physical:
            if trace["meta"]["waveform"]:
                for key in trace["meta"]["details"]["logical_ids"]:
                    physical_by_id[key] = trace["meta"]["waveform"]
        logical = []
        for event in point.logical_events:
            trace = _trace(
                event,
                tick,
                lane=(
                    "Native ARTIQ · declared timing"
                    if event.kind == "native_timed"
                    else None
                ),
                role=("structure" if event.kind == "native_timed" else None),
            )
            trace["meta"]["waveform"] = next(
                (
                    physical_by_id[key]
                    for key in event.logical_ids
                    if key in physical_by_id
                ),
                None,
            )
            logical.append(trace)
        if lifecycle is not None:
            native = lifecycle["points"][point.index]
            physical.extend(
                _native_stage_trace(call, tick, origin_us=point.origin_mu * tick * 1e6)
                for call in [*native.get("timed", ()), *native["suffix"]]
            )
            # Timed stages already exist in logical IR; add only the suffix.
            logical.extend(_native_stage_trace(call, tick) for call in native["suffix"])
        result.append(
            {
                "index": point.index,
                "coordinates": _jsonable(point.coordinates),
                "axis_indices": list(point.axis_indices),
                "execution_layout": execution[point.index],
                "origin_mu": point.origin_mu,
                "origin_us": point.origin_mu * tick * 1e6,
                "duration_us": point.duration_mu * tick * 1e6,
                "logical": logical,
                "physical": physical,
                "logical_events": _jsonable(point.logical_events),
                "physical_events": _jsonable(point.physical_events),
            }
        )
        if lifecycle is not None:
            result[-1]["native_lifecycle"] = lifecycle["points"][point.index]
            result[-1]["time_reference"] = lifecycle["time_reference"]
    return result


_APPLICATION_JS = r"""
const PulseSheet = (() => {
  const factors = {ns:1000, 'µs':1, ms:0.001};
  const escapes = {'&':'&amp;','<':'&lt;','>':'&gt;','%':'&#37;'};
  const esc = v => String(v).replace(/[&<>%]/g, c => escapes[c]);
  const number = value => Number(value.toPrecision(10)).toString();
  function timeGrid(range) {
    const target=(range[1]-range[0])/8;
    if(!Number.isFinite(target)||target<=0)return {major:1,minor:0.2};
    const scale=10**Math.floor(Math.log10(target));
    const step=([1,2,5,10].find(value=>value>=target/scale)||10)*scale;
    return {major:step,minor:step/5};
  }
  function executionIndex(pointIndex, shot, pointCount, shots, mode, layout=null) {
    if(layout && layout.scope==='chunk') {
      return layout.offset+(mode==='cyclic'?shot*layout.point_count+layout.local_point:
        layout.local_point*shots+shot);
    }
    return mode==='cyclic'?shot*pointCount+pointIndex:pointIndex*shots+shot;
  }
  function placeTraces(traces, entry, point, view) {
    const shift=entry.offset_s*1e6-(view==='physical'?point.origin_us:0);
    return traces.map(trace=>{
      const details={...trace.meta.details,
        local_start_us:trace.meta.details.start_us-(view==='physical'?point.origin_us:0),
        local_end_us:trace.meta.details.end_us-(view==='physical'?point.origin_us:0),
        start_us:trace.meta.details.start_us+shift,
        end_us:trace.meta.details.end_us+shift,shot:entry.shot,
        execution_index:entry.ordinal,point:entry.point};
      const meta={...trace.meta,shot:entry.shot,execution_index:entry.ordinal,
        point_index:entry.point,coordinates:point.coordinates,details};
      const params=details.parameters, summary=[
        ['f',params.frequency??params.frequency_hz,' Hz'],
        ['A',params.amplitude,''],['φ',params.phase??params.phase_rad,' rad']]
        .filter(([,value])=>typeof value==='number')
        .map(([label,value,suffix])=>label+'='+number(value)+suffix).join(' · ');
      const hover='<b>'+esc(details.signal)+' · '+esc(details.kind)+'</b><br>'+
        'Point '+entry.point+' · shot '+entry.shot+' · execution '+entry.ordinal+'<br>'+
        number(details.start_us)+'–'+number(details.end_us)+' µs · Δ '+
        number(details.duration_us)+' µs'+(summary?'<br>'+summary:'')+'<extra></extra>';
      return {...trace,x:trace.x.map(value=>value+shift),meta,hovertemplate:hover};
    });
  }
  function blockTrace(trace, unit) {
    const meta=trace.meta, start=trace.x[0]*factors[unit], end=trace.x[1]*factors[unit];
    const shared={name:trace.name,showlegend:false,hovertemplate:trace.hovertemplate,
      customdata:[meta],meta,marker:{color:meta.color},y:[esc(meta.lane)]};
    if(end===start) return {...shared,type:'scatter',mode:'markers',x:[start],
      marker:{color:meta.color,size:10,
      symbol:meta.role==='frame'?'diamond':'line-ns-open',line:{width:2}},
      cliponaxis:false};
    return {...shared,type:'bar',orientation:'h',base:[start],x:[end-start],
    width:meta.role==='control'?0.34:0.60,
      text:[esc(meta.label)],textposition:'inside',insidetextanchor:'middle',
      constraintext:'inside',
      textfont:{size:11,
      color:meta.role==='wait'||meta.role==='structure'?'#304344':'#ffffff'},
      marker:{color:meta.color,line:{color:meta.color,width:1}},
      opacity:meta.role==='wait'?0.55:meta.role==='structure'?0.6:1};
  }
  function waveformURL(point, meta, path='waveform.html') {
    const query=new URLSearchParams({point:String(meta.point_index??point),
      ...(meta.shot===undefined?{}:{shot:String(meta.shot)}),
      ...(meta.execution_index===undefined?{}:{execution:String(meta.execution_index)}),
      ...(meta.waveform||{})});
    return path+'?'+query.toString();
  }
  function visibleTraces(point, view, drivers, frames, search) {
    const query=search.trim().toLowerCase();
    return point[view].filter(t=>(drivers||t.meta.role!=='control')&&
      (frames||t.meta.role!=='frame')&&(!query||
        (t.meta.lane+' '+t.meta.label+' '+t.meta.engine)
          .toLowerCase().includes(query)));
  }
  function mount(points, shots, mode, waveformPath=null, executionAxis=null) {
    const el=id=>document.getElementById(id), graph=el('timeline');
    let selected=null, shown=[], executions=[], dragging='pan';
    const byIndex=new Map(points.map(point=>[point.index,point]));
    const unit=()=>el('unit').value||'µs';
    const bounds=()=>{
      if(!executions.length)return [0,1*factors[unit()]];
      let lo=Math.min(...executions.map(e=>e.start_s))*1e6*factors[unit()];
      let hi=Math.max(...executions.map(e=>e.end_s))*1e6*factors[unit()];
      for(const trace of shown)for(const x of trace.x){
        lo=Math.min(lo,x*factors[unit()]);hi=Math.max(hi,x*factors[unit()]);
      }
      const span=Math.max(hi-lo,1e-3*factors[unit()]);
      return [lo,hi+span*0.025];
    };
    function updateGrid(range) {
      const grid=timeGrid(range);
      if(graph.layout.xaxis.dtick!==grid.major||
          graph.layout.xaxis.minor.dtick!==grid.minor){
        Plotly.relayout(graph,{'xaxis.dtick':grid.major,
          'xaxis.minor.dtick':grid.minor});
      }
    }
    function setWindow(range) {
      if(!Array.isArray(range)||range.length!==2||!range.every(Number.isFinite)||
          !Number.isFinite(range[1]-range[0])||range[1]<=range[0]){
        el('window-status').textContent=
          'Enter a finite start and an end greater than start.';
        return false;
      }
      el('window-status').textContent='';
      el('window-start').value=number(range[0]);el('window-end').value=number(range[1]);
      el('viewport').textContent=number(range[1]-range[0])+' '+unit()+' window';
      const grid=timeGrid(range);
      Plotly.relayout(graph,{'xaxis.range':range,'xaxis.autorange':false,
        'xaxis.dtick':grid.major,'xaxis.minor.dtick':grid.minor});return true;
    }
    function select(meta) {
      selected=meta;
      el('details-title').textContent=meta.label;
      el('details-engine').textContent=meta.engine+' · '+meta.lane;
      const d=meta.details, params=d.parameters, body=el('details-values');
      body.replaceChildren();
      const fields=[['Point',meta.point_index],
        ['Coordinates',JSON.stringify(meta.coordinates)],
        ['Shot',meta.shot],['Execution index',meta.execution_index],
        ['Start',number(d.start_us)+' µs'],['End',number(d.end_us)+' µs'],
        ['Local start from T0',number(d.local_start_us)+' µs'],
        ['Duration',number(d.duration_us)+' µs'],['Frequency',
        params.frequency??params.frequency_hz],
        ['Amplitude',params.amplitude],['Phase (rad)',params.phase??params.phase_rad],
        ['Frame F (rad)',params.frame_phase],['Rz source diagnostic (rad)',
        params.virtual_z_phase],
        ['Carrier reference',params.phase_evolution?.carrier_reference_kind??
          params.phase_reference?.mode],
        ['Carrier reference from T0 (s)',
          params.phase_evolution?.carrier_reference_time_s],
        ['Logical IDs',(d.logical_ids||[]).join(', ')],['Source',d.source]];
      for(const [key,value] of fields){if(value===undefined)continue;
        const dt=document.createElement('dt'),dd=document.createElement('dd');
        dt.textContent=key;dd.textContent=String(value);body.appendChild(dt);
        body.appendChild(dd);}
      el('details-json').textContent=JSON.stringify(d,null,2);
      const link=el('waveform-link');link.hidden=!meta.waveform||!waveformPath;
      link.href=waveformPath?waveformURL(meta.point_index,meta,waveformPath):'#';
      el('focus-pulse').disabled=false;
      el('details-empty').hidden=true;el('details-content').hidden=false;
    }
    function render() {
      const view=el('view').value||'physical';
      const allPoints=el('point').value==='all'||el('point').value==='';
      const allShots=el('shot').value!=='single';
      const shot=Math.max(0,Math.min(shots-1,
        Math.trunc(Number(el('shot-index').value)||0)));
      el('shot-index').value=shot;el('shot-index-label').hidden=allShots;
      const start=Math.max(0,Math.trunc(Number(el('execution-start').value)||0));
      const count=Math.max(1,Math.min(50,
        Math.trunc(Number(el('execution-count').value)||50)));
      el('execution-start').value=start;el('execution-count').value=count;
      const selection=ViewerExecution.selectExecutions(executionAxis,{start,count,
        point:allPoints?null:Number(el('point').value),shot:allShots?null:shot,
        projection:'control'});
      executions=selection.entries;
      shown=executions.flatMap(entry=>{
        const point=byIndex.get(entry.point);
        return placeTraces(visibleTraces(point,view,el('drivers').checked,
          el('frames').checked,el('search').value),entry,point,view);
      });
      const ordinals=executions.map(e=>e.ordinal);
      el('selection').textContent=executions.length+' executions shown · '+mode+
        ' averaging'+(ordinals.length?
          ' · execution '+ordinals[0]+'–'+ordinals.at(-1):'')+
        (selection.truncated?' · more executions available in Advanced':'');
      el('reference').textContent='Consecutive known control windows, '+
        'not measured wall-clock time. '+
        'Host intervals are unknown and omitted at dashed execution boundaries; '+
        'an untimed native prefix is also omitted. '+
        'No pulse, delay or known card tail is shortened. '+
        'Each execution replays its own compiled phase reference; '+
        'concatenation changes only display coordinates. '+
        (view==='physical'?'Physical resource view includes modeled output timing.':
          'Logical view shows DSL scheduling; '+
          'rectangle height does not encode amplitude.')+
        ' Click an operation for complete parameters and source. '+
        'Execution/point/shot indices are zero-based.';
      const lanes=[...new Set(shown.map(t=>esc(t.meta.lane)))];
      el('event-count').textContent=shown.length+' events';
      el('lane-count').textContent=lanes.length+' lanes';
      el('duration-stat').textContent=number(executions.reduce(
        (sum,e)=>sum+e.end_s-e.start_s,0)*1e6)+' µs';
      const grid=timeGrid(bounds());
      const layout={paper_bgcolor:'#fffefa',plot_bgcolor:'#fffefa',font:{
      family:'Segoe UI, sans-serif',color:'#293331'},
        height:Math.max(380,lanes.length*43+140),margin:{l:158,
        r:25,t:60,b:55},
        barmode:'overlay',bargap:0.28,showlegend:false,hovermode:'closest',
        dragmode:dragging,
        hoverlabel:{bgcolor:'#fffefa',font:{family:'Consolas, monospace',size:11}},
        uniformtext:{minsize:9,mode:'hide'},
        xaxis:{title:{text:'Consecutive compiled time ('+unit()+')'},side:'top',
        range:bounds(),
        showgrid:true,gridcolor:'#cdd6c7',gridwidth:1,zeroline:false,
          tick0:0,dtick:grid.major,
          minor:{showgrid:true,gridcolor:'#e6ebe1',gridwidth:0.6,
            griddash:'dot',tick0:0,dtick:grid.minor},
          tickformat:'.8~g',ticks:'outside',ticklen:5,rangeslider:{visible:true,
          thickness:0.09,bgcolor:'#f1f3ed',bordercolor:'#d4dbcf',borderwidth:1}},
        yaxis:{type:'category',categoryorder:'array',categoryarray:lanes,
        autorange:'reversed',fixedrange:true,
          showgrid:true,gridcolor:'#edf0e8',ticks:'',automargin:true},
        shapes:executions.map(entry=>({type:'line',xref:'x',yref:'paper',
          x0:entry.start_s*1e6*factors[unit()],x1:entry.start_s*1e6*factors[unit()],
          y0:0,y1:1,line:{color:'#82928c',width:1,dash:'dot'}})),
        annotations:executions.filter((_,i)=>i%Math.max(1,Math.ceil(executions.length/12))===0)
          .map(entry=>({xref:'x',yref:'paper',x:entry.start_s*1e6*factors[unit()],
            y:1.04,text:'P'+entry.point+' / S'+entry.shot,showarrow:false,
            xanchor:'left',font:{size:10,color:'#56665e'}}))};
      Plotly.react(graph,shown.map(t=>blockTrace(t,unit())),layout,
        {responsive:true,displaylogo:false,scrollZoom:true,doubleClick:'reset',
        modeBarButtonsToRemove:['select2d','lasso2d','autoScale2d']});
      const range=bounds();el('window-start').value=number(range[0]);
      el('window-end').value=number(range[1]);
      el('viewport').textContent=number(range[1]-range[0])+' '+unit()+' window';
      el('empty-lanes').hidden=shown.length>0;
      const body=el('events');body.replaceChildren();
      let rows=0;
      for(const entry of executions){
        const point=byIndex.get(entry.point);
        for(const event of point[view+'_events']){
          if(rows++>=500)break;
          const row=document.createElement('tr');
          for(const value of [entry.ordinal,entry.point,entry.shot,
            event.kind,event.signal,
            event.start_mu,event.duration_mu,
            JSON.stringify(event.params)+' | '+event.source]){
            const cell=document.createElement('td');cell.textContent=String(value);
            row.appendChild(cell);
          }
          body.appendChild(row);
        }
        if(rows>=500)break;
      }
      el('table-note').textContent=
        'Raw local program coordinates for the displayed executions. '+
        'Showing up to 500 events; select any block for its complete fields.';
      selected=null;el('details-empty').hidden=false;
      el('details-content').hidden=true;el('focus-pulse').disabled=true;
    }
    for(const id of ['point','view','shot','shot-index','unit','drivers',
    'frames','execution-start','execution-count'])el(id).addEventListener('change',render);
    el('search').addEventListener('input',render);
    el('reset').addEventListener('click',()=>setWindow(bounds()));
    el('apply-window').addEventListener('click',
    ()=>setWindow([Number(el('window-start').value),Number(el('window-end').value)]));
    for(const [id,factor] of [['zoom-in',0.5],['zoom-out',
    2]])el(id).addEventListener('click',()=>{
      const range=graph.layout.xaxis.range||bounds(),middle=(range[0]+range[1])/2,
      half=(range[1]-range[0])*factor/2;setWindow([middle-half,middle+half]);});
    for(const [id,mode] of [['pan','pan'],['zoom',
    'zoom']])el(id).addEventListener('click',()=>{
      dragging=mode;Plotly.relayout(graph,{dragmode:mode});
      el('pan').setAttribute('aria-pressed',String(mode==='pan'));
      el('zoom').setAttribute('aria-pressed',String(mode==='zoom'));});
    el('focus-pulse').addEventListener('click',()=>{if(!selected)return;
    const d=selected.details,
      pad=Math.max((d.end_us-d.start_us)*0.15,0.001);
      setWindow([(d.start_us-pad)*factors[unit()],(d.end_us+pad)*factors[unit()]]);});
    graph.on('plotly_click',event=>{const meta=event.points?.[0]?.customdata;
    if(meta)select(meta);});
    graph.on('plotly_relayout',event=>{
      const range=event['xaxis.range']||(event['xaxis.autorange']?
        graph._fullLayout?.xaxis.range||bounds():[event['xaxis.range[0]'],
        event['xaxis.range[1]']]);
      if(range.every(Number.isFinite)){el('window-start').value=number(range[0]);
      el('window-end').value=number(range[1]);
      el('viewport').textContent=number(range[1]-range[0])+' '+unit()+' window';
      updateGrid(range);}});
    render();return {render,select,setWindow};
  }
  return {blockTrace,waveformURL,visibleTraces,placeTraces,
    executionIndex,timeGrid,mount};
})();
if(typeof globalThis!=='undefined')globalThis.PulseSheet=PulseSheet;
"""


_STYLE = """
:root{
color-scheme:light;
--ink:#293331;
--muted:#6b7770;
--paper:#f3f3eb;
--surface:#fffefa;
--line:#dce1d5;
--teal:#087f82;
--blue:#3562a6;
--amber:#b97819}

*{
box-sizing:border-box}
body{
margin:0;
background:var(--paper);
color:var(--ink);
font:14px/1.5 'Segoe UI',
Arial,
sans-serif}

button,
input,
select{
font:inherit}
button,
a,
input,
select,
summary{
outline-offset:3px}
button:focus-visible,
a:focus-visible,
input:focus-visible,
select:focus-visible,
summary:focus-visible{
outline:2px solid var(--teal)}

.masthead{
background:#293331;
color:#f7f8f1;
padding:13px 28px;
display:flex;
align-items:center;
justify-content:space-between}
.brand{
font-family:Consolas,
monospace;
font-size:13px;
letter-spacing:.12em}
.masthead small{
color:#bdcac1;
font-size:11px;
letter-spacing:.08em}

main{
max-width:1760px;
margin:auto;
padding:26px 28px 40px}
.heading{
display:flex;
align-items:flex-start;
justify-content:space-between;
gap:20px;
margin-bottom:20px}
.eyebrow{
color:var(--teal);
font:11px Consolas,
monospace;
letter-spacing:.13em;
text-transform:uppercase}
h1{
font-size:27px;
font-weight:600;
letter-spacing:-.7px;
margin:3px 0 4px}
.subtitle,
.note{
color:var(--muted);
font-size:12px;
margin:0}
.stats{
display:flex;
gap:24px;
text-align:right;
padding-top:8px}
.stats strong{
display:block;
font:19px Consolas,
monospace;
font-weight:500}
.stats span{
font-size:10px;
letter-spacing:.09em;
color:var(--muted);
text-transform:uppercase}

.workspace{
border:1px solid var(--line);
background:var(--surface);
border-radius:9px;
overflow:hidden}
.advanced>summary{padding:12px 18px;cursor:pointer;color:var(--muted);font-size:12px}
.controls{
display:flex;
gap:12px;
flex-wrap:wrap;
align-items:end;
padding:15px 18px;
border-bottom:1px solid var(--line);
background:#fafbf5}
.control{
display:flex;
flex-direction:column;
gap:4px;
color:var(--muted);
font-size:10px;
font-weight:600;
letter-spacing:.04em;
text-transform:uppercase}
.control select,
.control input{
height:34px;
color:var(--ink);
text-transform:none;
font-size:12px;
font-weight:400;
letter-spacing:0}
.control.grow{
flex:1;
min-width:180px}
.control.grow select{
width:100%}
select,
input,
button,
.button{
border:1px solid #cfd7ca;
border-radius:4px;
background:var(--surface);
padding:6px 9px;
min-height:32px;
color:var(--ink)}
select{
max-width:520px}
input[type=number]{
width:88px}
input[type=search]{
width:180px}
button{
cursor:pointer;
font-size:12px}
button:hover,
.button:hover{
background:#e9eee4}
button[aria-pressed=true]{
background:#dcece7;
border-color:#a2c4b9;
color:#135e56}
button:disabled{
opacity:.45;
cursor:default}

.toolbar{
display:flex;
align-items:center;
gap:7px;
flex-wrap:wrap;
padding:10px 18px;
border-bottom:1px solid var(--line)}
.toolbar .divider{
width:1px;
height:21px;
background:var(--line);
margin:0 5px}
.toolbar label{
font-size:11px;
color:var(--muted);
display:flex;
align-items:center;
gap:5px}
.toolbar input[type=checkbox]{
accent-color:var(--teal);
min-height:auto}
.toolbar .range{
width:100px;
min-height:30px;
font:11px Consolas,
monospace}
.toolbar .spacer{
flex:1}
.toolbar select{
min-height:30px;
font-size:11px}
#viewport{
font:11px Consolas,
monospace;
color:var(--muted)}

.chart-layout{
display:grid;
grid-template-columns:minmax(0,
1fr) 290px;
min-height:450px}
.chart-area{
min-width:0;
overflow:hidden}
.selection{
padding:10px 18px 0;
font:11px Consolas,
monospace;
color:var(--muted);
overflow-wrap:anywhere}
.plot-wrap{
max-height:840px;
overflow:auto}
#timeline{
width:100%}
.legend{
padding:10px 18px 12px;
display:flex;
flex-wrap:wrap;
gap:17px;
border-top:1px solid var(--line);
color:var(--muted);
font-size:10px}
.legend span{
display:flex;
gap:6px;
align-items:center}
.swatch{
width:12px;
height:8px;
border-radius:1px;
background:var(--blue)}
.swatch.awg{
background:var(--teal)}
.swatch.frame{
background:var(--amber);
width:7px;
height:7px;
transform:rotate(45deg)}
.swatch.wait{
background:#a5aaa8}
.swatch.driver{
background:#867793}

.inspector{
border-left:1px solid var(--line);
padding:18px;
background:#fafbf5;
min-width:0}
.inspector h2{
font-size:14px;
margin:0 0 4px;
font-weight:600}
.inspector .overline{
font:10px Consolas,
monospace;
color:var(--muted);
letter-spacing:.1em;
margin-bottom:17px}
.empty{
padding:28px 0;
color:var(--muted);
font-size:12px}
.empty b{
display:block;
color:var(--ink);
font-size:13px;
font-weight:500;
margin-bottom:8px}
.inspector dl{
margin:20px 0;
font-size:11px}
.inspector dt{
color:var(--muted);
margin-top:12px}
.inspector dd{
font-family:Consolas,
monospace;
margin:2px 0 0;
overflow-wrap:anywhere}
.inspector .button{
display:block;
text-align:center;
text-decoration:none;
margin-top:10px;
font-size:12px}
#focus-pulse{
width:100%}
#details-json{
max-height:240px;
font-size:10px}
#details-engine{
font-size:11px;
color:var(--muted);
overflow-wrap:anywhere}

.reference{
padding:11px 18px;
border-top:1px solid var(--line);
font-size:11px;
color:var(--muted)}
.support{
display:grid;
grid-template-columns:1fr 1fr;
gap:16px;
margin-top:18px}
.panel{
border:1px solid var(--line);
background:var(--surface);
border-radius:7px;
padding:16px 18px}
.panel details+details{
margin-top:12px}
summary{
cursor:pointer;
font-size:12px;
font-weight:600}
.panel details details{
margin:10px 0 0 12px}
pre{
font:11px/1.55 Consolas,
monospace;
white-space:pre-wrap;
overflow-wrap:anywhere;
max-height:300px;
overflow:auto;
background:#f0f3eb;
border-radius:4px;
padding:10px}
.table{
max-height:320px;
overflow:auto}
table{
border-collapse:collapse;
width:100%;
font:10px Consolas,
monospace}
th,
td{
padding:7px;
text-align:left;
border-bottom:1px solid var(--line);
vertical-align:top}
td:last-child{
max-width:500px;
overflow-wrap:anywhere}
th{
position:sticky;
top:0;
background:var(--surface)}
.table-panel{
margin-top:16px}
.help{
font-size:10px;
color:var(--muted);
margin:9px 0 0}
#window-status{
color:#a24d2c;
font-size:11px}
#empty-lanes{
text-align:center;
color:var(--muted);
padding:18px}
[hidden]{
display:none!important}

@media(max-width:1000px){
.chart-layout{
grid-template-columns:minmax(0,
1fr) 240px}
.stats{
gap:14px}
main{
padding:18px}
.masthead{
padding:12px 18px}
.toolbar .range{
width:80px}
}

@media(max-width:760px){
.heading{
display:block}
.stats{
text-align:left;
margin-top:13px}
.chart-layout{
grid-template-columns:1fr}
.inspector{
border-left:0;
border-top:1px solid var(--line)}
.support{
grid-template-columns:1fr}
.control.grow{
width:100%}
main{
padding:12px}
.masthead small{
display:none}
.toolbar .spacer{
display:none}
.toolbar{
gap:6px}
h1{
font-size:23px}
input[type=search]{
width:150px}
.controls{
padding:12px}
}

"""


def visualize(compiled, path=None, *, waveform_path=None):
    """Save an offline pulse sheet, preserving the existing visualization API.

    Rectangle width represents duration, not sampled waveform amplitude. Shots
    select host execution order; inter-shot gaps remain unspecified.
    """
    if hasattr(compiled, "assert_integrity"):
        compiled.assert_integrity()
    if waveform_path is not None:
        waveform_path = str(waveform_path).replace("\\", "/")
        if ":" in waveform_path or waveform_path.startswith("/"):
            raise ValueError("waveform_path must be a relative local file path")
        waveform_path = quote(waveform_path, safe="/.-_")
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
    except ImportError as exc:
        raise RuntimeError("Visualization requires plotly; run uv sync") from exc
    from .viewer_execution import execution_axis

    payload = _payload(compiled)
    execution_data = execution_axis(compiled)
    execution_script = asset_text("viewer_execution.js")
    if not payload:
        raise ValueError("Cannot visualize an experiment with no compiled points")
    plot = pio.to_html(
        go.Figure(),
        include_plotlyjs=True,
        full_html=False,
        div_id="timeline",
        config={"responsive": True, "displaylogo": False},
    )

    def json_html(value, indent=None):
        return escape(json.dumps(value, ensure_ascii=False, indent=indent))

    options = "".join(
        f'<option value="{i}">{point["index"]}: '
        f"{json_html(point['coordinates'])}</option>"
        for i, point in enumerate(payload)
    )
    axes = "".join(
        f"<details open><summary>Sweep {escape(str(axis['name']))} "
        f"({escape(str(axis.get('unit', '')))})</summary>"
        f"<pre>{json_html(_jsonable(axis['values']), indent=2)}</pre></details>"
        for axis in compiled.axes
    )
    title = escape(compiled.name)
    diagnostics = escape(
        json.dumps(_jsonable(compiled.diagnostics), ensure_ascii=False, indent=2)
    )
    provenance = escape(
        json.dumps(_jsonable(compiled.provenance), ensure_ascii=False, indent=2)
    )
    from .native_view import native_lifecycle_html

    native_panel = native_lifecycle_html(compiled)
    duration_label = "Known span shown"
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Pulse sheet</title>
<style>{_STYLE}</style>
</head>
<body>
<header class="masthead">
<div class="brand">TAIYI / PULSE SHEET</div>
<small>COMPILED EXPERIMENT · OFFLINE INSPECTION</small>
</header>
<main>
<div class="heading">
<div>
<div class="eyebrow">Sequence inspector</div>
<h1>{title}</h1>
<p class="subtitle">All points and shots in execution order,
on the same resource lanes.</p>
</div>
<div class="stats">
<div>
<strong id="duration-stat">—</strong>
<span>{duration_label}</span>
</div>
<div>
<strong id="lane-count">—</strong>
<span>Visible resources</span>
</div>
<div>
<strong id="event-count">—</strong>
<span>Visible operations</span>
</div>
</div>
</div>
<section class="workspace" aria-label="Pulse timeline">
<div class="toolbar">
<button id="reset">Full view</button>
<span>Scroll to zoom · drag to pan · click a pulse for details</span>
<span class="spacer"></span><span id="viewport"></span>
</div>
<details class="advanced">
<summary>Advanced · filters, units and precise window</summary>
<div class="controls">
<label class="control">View<select id="view">
<option value="physical" selected>Physical resources</option>
<option value="logical">Logical signals</option>
</select></label>
<label class="control grow">Sweep point<select id="point">
<option value="all" selected>All points</option>{options}</select></label>
<label class="control">Shots<select id="shot">
<option value="all" selected>All shots</option>
<option value="single">One shot index</option>
</select></label>
<label id="shot-index-label" class="control" hidden>Shot index
<input id="shot-index" type="number" min="0" max="{compiled.shots - 1}" value="0">
</label>
<label class="control">First execution index
<input id="execution-start" type="number" min="0" value="0"></label>
<label class="control">Executions to show (up to 50)
<input id="execution-count" type="number" min="1" max="50" value="50"></label>
<label class="control">Find a signal<input id="search" type="search"
placeholder="Signal, card, operation…"></label>
</div>
<div class="toolbar">
<button id="pan" aria-pressed="true" title="Drag the time axis">Pan</button>
<button id="zoom" aria-pressed="false" title="Drag to select a time window">
Box zoom</button>
<button id="zoom-in" aria-label="Zoom in">＋</button>
<button id="zoom-out" aria-label="Zoom out">−</button>
<label>From<input class="range" id="window-start" type="number" step="any" value="0">
</label>
<label>to<input class="range" id="window-end" type="number" step="any" value="1">
</label>
<select id="unit" aria-label="Time unit"><option>ns</option>
<option selected>µs</option><option>ms</option></select>
<button id="apply-window">Apply</button>
<label><input id="frames" type="checkbox" checked>Frames</label>
<label><input id="drivers" type="checkbox">Driver calls</label>
</div>
</details>
<div id="window-status" role="status">
</div>
<div class="chart-layout">
<div class="chart-area">
<div id="selection" class="selection">
</div>
<div class="plot-wrap">{plot}</div>
<div id="empty-lanes" hidden>No events match these filters.</div>
<div class="legend">
<span>
<i class="swatch">
</i>ARTIQ output</span>
<span>
<i class="swatch awg">
</i>Spectrum output</span>
<span>
<i class="swatch frame">
</i>Frame update</span>
<span>
<i class="swatch wait">
</i>Wait / reservation / zero</span>
<span>
<i class="swatch driver">
</i>Driver operation</span>
</div>
</div>
<aside class="inspector" aria-label="Selected pulse">
<div class="overline">OPERATION DETAILS</div>
<div id="details-empty" class="empty">
<b>Select a pulse block</b>Click an operation to inspect its exact boundaries,
parameters and source.<p>Scroll to zoom. Drag to pan. Use Box zoom for a precise
interval.</p>
</div>
<div id="details-content" hidden>
<h2 id="details-title">
</h2>
<div id="details-engine">
</div>
<dl id="details-values">
</dl>
<button id="focus-pulse" disabled>Focus this operation</button>
<a id="waveform-link" class="button" href="waveform.html" hidden>Inspect waveform ↗</a>
<details>
<summary>All event fields</summary>
<pre id="details-json">
</pre>
</details>
</div>
</aside>
</div>
<div id="reference" class="reference">
</div>
</section>
<details class="panel"><summary>Native lifecycle and unknown intervals</summary>
{native_panel}</details>
<div class="support">
<section class="panel">
<details open>
<summary>Loop structure</summary>
<details open>
<summary>Average: {escape(compiled.averaging_mode)} · {compiled.shots} shots per
point</summary>{axes}
<p class="help">Each selected shot uses the same compiled within-point timeline. Host
inter-shot intervals are not fixed. Expand/collapse loops without expanding every
repetition.</p>
</details>
</details>
</section>
<section class="panel">
<details>
<summary>Compilation diagnostics</summary>
<pre>{diagnostics}</pre>
</details>
<details>
<summary>Configuration provenance</summary>
<pre>{provenance}</pre>
</details>
<p class="help">Compiled model only. This view does not establish ARTIQ compilation or
hardware timing acceptance.</p>
</section>
</div>
<section class="panel table-panel">
<details>
<summary>Selected event table</summary>
<p id="table-note" class="help">
</p>
<div class="table">
<table>
<thead>
<tr>
<th>Execution</th>
<th>Point</th>
<th>Shot</th>
<th>Kind</th>
<th>Signal</th>
<th>Local start (scheduler mu)</th>
<th>Duration (scheduler mu)</th>
<th>Parameters / source</th>
</tr>
</thead>
<tbody id="events">
</tbody>
</table>
</div>
</details>
</section>
</main>
<script>{execution_script}
{_APPLICATION_JS}
PulseSheet.mount(
  {_safe_json(payload)}, {int(compiled.shots)},
  {_safe_json(compiled.averaging_mode)}, {_safe_json(waveform_path)},
  {_safe_json(execution_data)}
);
</script>
</body>
</html>
'''
    destination = (
        Path(path)
        if path is not None
        else Path("output") / f"{compiled.name}_timeline.html"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return destination.resolve()
