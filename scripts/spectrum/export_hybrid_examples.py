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
            f'<td><a href="{name}/index.html">完整报告</a></td>'
            f'<td><a href="{name}/dsl_source.py">DSL</a></td>'
            f'<td><a href="{name}/pulse_sheet.html">Pulse sheet</a></td>'
            f'<td><a href="{name}/waveform.html">Waveform</a></td></tr>'
        )
    document = (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
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
<h1>混合控制实验查看器</h1>
<p>从完整 DSL 到矩形时序，再到短窗口的最终 DAC 样本。每份报告含两卡
segment / step table、ARTIQ 触发和逐 shot 相位参考。原有纯 ARTIQ 示例保留。</p>
<div class="scroll"><table><thead><tr><th>实验</th><th>扫描点</th>
<th>入口</th><th>源码</th><th>宏观时序</th><th>样本波形</th></tr></thead><tbody>
"""
        + "\n".join(rows)
        + """</tbody></table></div>
<p class="note">所有默认输出均为离线模型，脉冲面积与硬件延迟未经标定。
波形查看器不模拟模拟链路、AOM 或量子态；生成报告不会启动任何仪器。</p>
<p class="muted">Pulse sheet：悬停查看参数，单击查看详情，滚轮缩放/拖动平移；
点击 Spectrum 门的 waveform 链接进入对应采样窗口。
Waveform：窗口默认最多 100000 样本，不下采样 RF。使用现代 Edge/Chrome 打开。</p>
</html>"""
    )
    index = args.output / "index.html"
    index.write_text(document, encoding="utf-8")
    print(f"Suite index: {index.resolve()}")


if __name__ == "__main__":
    main()
