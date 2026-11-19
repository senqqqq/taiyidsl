"""Generate the separate hybrid experiment review suite, entirely offline."""

import argparse
import html
import runpy
from pathlib import Path

from taiyidsl import LabConfig

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    "ramsey_long_delay",
    "rabi",
    "spin_echo",
    "spin_locking",
    "allxy",
    "staged_workflow",
    "greenmot_ramsey",
    "parallel_clock_rydberg",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/artiq_spectrum"))
    args = parser.parse_args()
    examples = ROOT / "examples" / "artiq_spectrum"
    lab = LabConfig.load(examples / "lab_simulated.json")
    rows = []
    for name in CASES:
        if name == "parallel_clock_rydberg":
            source = ROOT / "examples" / "spectrum_hybrid.py"
            compiled = runpy.run_path(str(source))["build_three_channel"]()
        else:
            source = examples / f"{name}.py"
            builder = "build_demo" if name == "staged_workflow" else "build_experiment"
            exp = runpy.run_path(str(source))[builder]()
            compiled = lab.compile(exp)
        result = compiled.export_report(args.output / name, source_path=source)
        print(result["index"].resolve(), flush=True)
        rows.append(
            f"<tr><th>{html.escape(name)}</th><td>{len(compiled.points)}</td>"
            f'<td><a href="{name}/index.html">Full report</a></td>'
            f'<td><a href="{name}/dsl_source.py">DSL</a></td>'
            f'<td><a href="{name}/pulse_sheet.html">Pulse sheet</a></td>'
            f'<td><a href="{name}/waveform.html">Waveform</a></td></tr>'
        )
    document = (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaiyiDSL · Hybrid experiment viewers</title><style>
body{font:16px/1.7 "Segoe UI",sans-serif;background:#f6f7f3;color:#213337;
max-width:1100px;margin:50px auto;padding:0 24px}h1{font-weight:600}
.tag{font-size:12px;letter-spacing:.15em;color:#087f78}
table{border-collapse:collapse;width:100%;background:white;margin:28px 0}
th,td{text-align:left;padding:16px;border-bottom:1px solid #d6dedb}
th{font-family:Consolas,monospace;font-weight:500}a{color:#087f78}
.scroll{overflow:auto}.note{border-left:4px solid #b88430;padding:14px 20px;
background:#fff6e5}.muted{color:#617174;font-size:14px}
</style><div class="tag">TAIYIDSL / ARTIQ + SPECTRUM / OFFLINE</div>
<h1>Hybrid experiment viewers</h1>
<p>Follow the DSL source through the schedule to the final DAC samples. Each report
includes the segment and step tables for both cards, the ARTIQ trigger and each
shot's phase reference.</p>
<div class="scroll"><table><thead><tr><th>Experiment</th><th>Points</th>
<th>Report</th><th>Source</th><th>Timing</th><th>Samples</th></tr></thead><tbody>
"""
        + "\n".join(rows)
        + """</tbody></table></div>
<p class="note">These offline examples use uncalibrated pulse areas and device
latencies. The viewer models digital outputs. The analog chain, AOM response and
quantum state require separate measurements.</p>
<p class="muted">Hover over a pulse for its parameters, click for details, scroll
to zoom and drag to pan. Select a Spectrum gate's waveform link to inspect its
samples. The default exact window is limited to 100,000 samples. Open in Edge or
Chrome.</p>
</html>"""
    )
    index = args.output / "index.html"
    index.write_text(document, encoding="utf-8")
    print(f"Suite index: {index.resolve()}")


if __name__ == "__main__":
    main()
