"""Self-contained, offline Plotly timelines built from compiled execution plans."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from types import SimpleNamespace

from .artiq import _jsonable

_COLORS = {
    "play": "#2563eb",
    "delay": "#94a3b8",
    "reserve": "#64748b",
    "measure": "#08916d",
    "prepare": "#a855f7",
    "shift_phase": "#e57d17",
    "reset_frame": "#e57d17",
    "dds_config": "#a855f7",
    "dds_on": "#2563eb",
    "dds_off": "#64748b",
    "ttl_on": "#08916d",
    "ttl_off": "#64748b",
    "dds_output": "#2563eb",
    "ttl_output": "#08916d",
}


def _trace(event, tick_seconds):
    start = float(event.start_mu) * tick_seconds * 1e6
    duration = float(event.duration_mu) * tick_seconds * 1e6
    details = {
        "kind": event.kind,
        "signal": event.signal,
        "start_mu": event.start_mu,
        "duration_mu": event.duration_mu,
        "parameters": _jsonable(event.params),
        "source": event.source,
        "resources": list(getattr(event, "resources", ())),
        "logical_ids": list(getattr(event, "logical_ids", ())),
    }
    tooltip = "<br>".join(
        f"<b>{escape(str(k))}</b>: {escape(json.dumps(v, ensure_ascii=False))}"
        for k, v in details.items()
    )
    return {
        "type": "scatter",
        "x": [start, start + duration],
        "y": [event.signal, event.signal],
        "mode": "lines+markers" if duration else "markers",
        "line": {
            "width": 10 if duration else 0,
            "color": _COLORS.get(event.kind, "#64748b"),
        },
        "marker": {"size": 7, "color": _COLORS.get(event.kind, "#64748b")},
        "name": event.kind,
        "legendgroup": event.kind,
        "showlegend": False,
        "hovertemplate": tooltip + "<extra></extra>",
    }


def _payload(compiled):
    tick = float(compiled.setup.tick_seconds)
    return [
        {
            "index": point.index,
            "coordinates": _jsonable(point.coordinates),
            "axis_indices": list(point.axis_indices),
            "origin_mu": point.origin_mu,
            "logical": [_trace(e, tick) for e in point.logical_events],
            "physical": _physical_traces(point.physical_events, tick),
            "logical_events": _jsonable(point.logical_events),
            "physical_events": _jsonable(point.physical_events),
        }
        for point in compiled.points
    ]


def _physical_traces(events, tick_seconds):
    """Reconstruct gated output spans in addition to driver calls and edges."""
    opened, configuration, spans = {}, {}, []
    for event in sorted(events, key=lambda item: item.start_mu):
        if event.kind == "dds_config":
            configuration[event.signal] = dict(event.params)
        elif event.kind in {"dds_on", "ttl_on"}:
            opened[event.signal] = (event, dict(configuration.get(event.signal, {})))
        elif event.kind in {"dds_off", "ttl_off"} and event.signal in opened:
            start, params = opened.pop(event.signal)
            interval = SimpleNamespace(
                kind=event.kind[:3] + "_output",
                signal=event.signal,
                start_mu=start.start_mu,
                duration_mu=event.start_mu - start.start_mu,
                params=params,
                source=start.source,
                resources=getattr(start, "resources", ()),
                logical_ids=getattr(start, "logical_ids", ()),
            )
            spans.append(_trace(interval, tick_seconds))
    return spans + [_trace(event, tick_seconds) for event in events]


def visualize(compiled, path=None):
    """Save an offline HTML timeline; does not connect to or execute hardware.

    Shot selection changes provenance/order only: v1 has static within-point
    timelines and host-controlled, non-deterministic inter-shot gaps.
    """
    if hasattr(compiled, "assert_integrity"):
        compiled.assert_integrity()
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
    except ImportError as exc:
        raise RuntimeError(
            "Visualization requires plotly; install the project's "
            "locked dependencies with uv sync"
        ) from exc
    payload = _payload(compiled)
    if not payload:
        raise ValueError("Cannot visualize an experiment with no compiled points")
    fig = go.Figure(data=payload[0]["logical"])
    fig.update_layout(
        height=550,
        template="plotly_white",
        margin={"l": 120, "r": 30, "t": 35, "b": 65},
        xaxis={
            "title": "Time (µs)",
            "zeroline": True,
            "rangeslider": {"visible": True},
        },
        yaxis={"title": "Logical signal", "type": "category", "autorange": "reversed"},
        hovermode="closest",
        showlegend=False,
    )
    plot = pio.to_html(
        fig,
        include_plotlyjs=True,
        full_html=False,
        div_id="timeline",
        config={"responsive": True, "displaylogo": False},
    )

    def safe_json(value):
        return (
            json.dumps(value, ensure_ascii=False, allow_nan=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )

    point_options = "".join(
        f'<option value="{i}">{point["index"]}: '
        f"{escape(json.dumps(point['coordinates'], ensure_ascii=False))}</option>"
        for i, point in enumerate(payload)
    )
    axes = "".join(
        f"<details open><summary>Sweep {escape(str(axis['name']))} "
        f"({escape(str(axis.get('unit', '')))})</summary><pre>"
        f"{escape(json.dumps(_jsonable(axis['values']), ensure_ascii=False, indent=2))}"
        "</pre></details>"
        for axis in compiled.axes
    )
    diagnostics = escape(
        json.dumps(_jsonable(compiled.diagnostics), ensure_ascii=False, indent=2)
    )
    provenance = escape(
        json.dumps(_jsonable(compiled.provenance), ensure_ascii=False, indent=2)
    )
    title = escape(compiled.name)
    html = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><meta '
        'name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>"
        f"{title}"
        " — compiled pulse timeline</title>\n"
        "<style>\n"
        "body{font:15px system-ui,sans-serif;color:#172334;background:#f"
        "3f6fa;margin:0;padding:24px}\n"
        "main{max-width:1400px;margin:auto}h1{font-size:26px;margin-bott"
        "om:8px}.panel{background:white;border:1px solid "
        "#dce3ed;border-radius:10px;padding:20px;margin:16px 0}\n"
        ".controls{display:flex;gap:20px;flex-wrap:wrap;align-items:cent"
        "er}label{display:flex;gap:8px;align-items:center}select,input,b"
        "utton{padding:7px;border:1px solid "
        "#bcc7d6;border-radius:5px;font:inherit}\n"
        "select{max-width:650px}.note{color:#526176;line-height:1.55}det"
        "ails{margin:10px 0}summary{cursor:pointer;font-weight:600}pre{w"
        "hite-space:pre-wrap;word-break:break-word;font-size:12px;max-he"
        "ight:340px;overflow:auto;background:#f5f7fa;padding:12px}\n"
        "table{border-collapse:collapse;width:100%;font-size:12px}th,td{"
        "padding:7px;border-bottom:1px solid "
        "#e2e8f0;text-align:left}.table{max-height:350px;overflow:auto}\n"
        "</style></head><body><main>\n"
        "<h1>"
        f"{title}"
        "</h1>\n"
        '<p class="note">Compiled plan · offline model · ARTIQ '
        "compilation and hardware timing are not verified.</p>\n"
        '<section class="panel controls">\n'
        '<label>View <select id="view"><option value="logical">Logical '
        'signals</option><option value="physical">Physical '
        "resources</option></select></label>\n"
        '<label>Sweep point <select id="point">'
        f"{point_options}"
        "</select></label>\n"
        '<label>Shot <input id="shot" type="number" min="0" max="'
        f"{compiled.shots - 1}"
        '" value="0"></label>\n'
        '<button id="reset" type="button">Reset zoom</button>\n'
        "</section>\n"
        '<p id="selection" class="note"></p>\n'
        '<section class="panel">'
        f"{plot}"
        "</section>\n"
        '<p id="reference" class="note"></p>\n'
        '<section class="panel"><details open><summary>Loop '
        "structure</summary>\n"
        "<details open><summary>Average: "
        f"{escape(compiled.averaging_mode)}"
        " · "
        f"{compiled.shots}"
        " shots per point</summary>"
        f"{axes}"
        "\n"
        '<p class="note">Each selected shot uses the same compiled '
        "within-point timeline. Host inter-shot intervals are not "
        "fixed. Expand/collapse these loops to inspect the scan "
        "structure without expanding every "
        "repetition.</p></details></details>\n"
        "<details><summary>Selected event table</summary><div "
        'class="table"><table><thead><tr><th>Kind</th><th>Signal</th><th'
        ">Start (mu)</th><th>Duration (mu)</th><th>Parameters / "
        "source</th></tr></thead><tbody "
        'id="events"></tbody></table></div></details>\n'
        "<details><summary>Compilation diagnostics</summary><pre>"
        f"{diagnostics}"
        "</pre></details>\n"
        "<details><summary>Configuration provenance</summary><pre>"
        f"{provenance}"
        "</pre></details></section>\n"
        "</main><script>\n"
        "const points="
        f"{safe_json(payload)}"
        ";\n"
        "const shots="
        f"{int(compiled.shots)}"
        ", mode="
        f"{safe_json(compiled.averaging_mode)}"
        ";\n"
        "const pointEl=document.getElementById('point'), "
        "viewEl=document.getElementById('view'), "
        "shotEl=document.getElementById('shot');\n"
        "function render(){\n"
        " const i=Number(pointEl.value), p=points[i], "
        "view=viewEl.value;\n"
        " let shot=Math.max(0,Math.min(shots-1,Math.trunc(Number(shotEl."
        "value)||0)));shotEl.value=shot;\n"
        " const ordinal=mode==='cyclic'?shot*points.length+i:i*shots+sho"
        "t;\n"
        " document.getElementById('selection').textContent='Point "
        "'+p.index+' · shot '+shot+' · execution index '+ordinal+' · "
        "'+JSON.stringify(p.coordinates);\n"
        " document.getElementById('reference').textContent=view==='physi"
        "cal'?'Physical view: driver/RTIO model time including "
        "pre-roll; logical shot origin = '+p.origin_mu+' mu. Optical "
        "arrival times are not calibrated.':'Logical view: time "
        "relative to shot origin. Device setup operations are shown in "
        "the physical view.';\n"
        " const node=document.getElementById('timeline');\n"
        " Plotly.react(node,p[view],Object.assign({},node.layout,{yaxis:"
        "{title:{text:view==='logical'?'Logical signal':'Physical "
        "device'},type:'category',autorange:'reversed'},xaxis:{title:{te"
        "xt:'Time (µs)'},autorange:true,rangeslider:{visible:true}}}),{r"
        "esponsive:true,displaylogo:false});\n"
        " const body=document.getElementById('events');body.replaceChild"
        "ren();\n"
        " for(const e of p[view+'_events']){const "
        "row=document.createElement('tr');for(const val of "
        "[e.kind,e.signal,e.start_mu,e.duration_mu,JSON.stringify(e.para"
        "ms)+' | '+e.source]){const "
        "cell=document.createElement('td');cell.textContent=String(val);"
        "row.appendChild(cell);}body.appendChild(row);}\n"
        "}\n"
        "pointEl.addEventListener('change',render);viewEl.addEventListen"
        "er('change',render);shotEl.addEventListener('change',render);\n"
        "document.getElementById('reset').addEventListener('click',()=>P"
        "lotly.relayout('timeline',{'xaxis.autorange':true,'yaxis.autora"
        "nge':true}));render();\n"
        "</script></body></html>"
    )
    destination = (
        Path(path)
        if path is not None
        else Path("output") / f"{compiled.name}_timeline.html"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return destination.resolve()
