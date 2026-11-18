"""Offline code views of the real Spectrum adapter and resolved upload arguments.

No SDK import or instrument access is needed. Source snapshots are evidence of
the implementation at export time, not a record of a hardware run.
"""

from __future__ import annotations

import ast
import hashlib
import html

from .resources import RUNTIME_SOURCES, runtime_source_bytes


def card_execution(item):
    """Resolve the manifest exactly as SpcmAdapter.upload maps segment slots."""
    contract, program = item["contract"], item["program"]
    if program.get("kind") in {"dds", "dds_bank"}:
        from .spectrum_dds_report import dds_execution

        return dds_execution(item)
    slots = {segment["id"]: slot for slot, segment in enumerate(program["segments"])}
    configuration = [
        {"setting": name, "value": value, "source": source}
        for name, value, source in (
            ("serial_number", contract["serial_number"], "contract"),
            ("clock.mode", "SPC_CM_EXTREFCLOCK", "adapter; checked each shot"),
            ("clock.reference_clock [Hz]", contract["reference_clock_hz"], "contract"),
            ("clock.sample_rate [Hz]", contract["sample_rate_hz"], "contract"),
            ("card_mode", "SPC_REP_STD_SEQUENCE", "adapter"),
            ("active_channels", contract["active_channels"], "contract"),
            ("output_range [mV]", contract["output_range_mv"], "contract"),
            ("output offset / stop level", "0 / SPCM_STOPLVL_ZERO", "adapter"),
            ("ARTIQ trigger_device", contract["trigger_device"], "contract"),
            ("trigger.or_mask / and_mask", "SPC_TMASK_EXT0 / 0", "adapter"),
            ("trigger.ext0_mode / coupling", "SPC_TM_POS / COUPLING_DC", "adapter"),
            (
                "trigger.ext0_level0 [mV]",
                contract.get("trigger_level_mv", 1000),
                "contract" if "trigger_level_mv" in contract else "adapter default",
            ),
            (
                "trigger termination [ohm]",
                contract.get("trigger_termination_ohm", 50),
                "contract"
                if "trigger_termination_ohm" in contract
                else "adapter default",
            ),
            ("trigger.delay [samples]", 0, "adapter; programmable extra delay"),
            (
                "trigger_latency_samples",
                contract["trigger_latency_samples"],
                "contract timing model; NOT trigger.delay",
            ),
        )
    ]
    uploads = [
        {
            "segment_id": segment["id"],
            "hardware_slot": slots[segment["id"]],
            "samples_per_channel": segment["sample_count"],
            "sha256": segment["sha256"],
        }
        for segment in program["segments"]
    ]
    steps = [
        {
            "step_index": step["index"],
            "next_step_index": step["next_step"],
            "segment_index": slots[step["segment_id"]],
            "loops": step["loops"],
            "flags": "SPCSEQ_END" if step["end"] else "SPCSEQ_ENDLOOPALWAYS",
        }
        for step in program["steps"]
    ]
    # This is a review listing, never executed or substituted for the adapter.
    lines = [
        "# Resolved upload arguments; inspection only, NOT a standalone runner.",
        "# Runtime guards and SDK readbacks: see the exact adapter source above.",
        f"sequence.max_segments({program['memory']['segment_slots']!r})",
        "",
        "# 1. Waveform DMA: loader validates/materializes the manifest recipes.",
        "# Report .bin files are review copies, not the runner's input files.",
    ]
    for upload in uploads:
        lines += [
            f"# segment ID {upload['segment_id']} -> hardware slot "
            f"{upload['hardware_slot']}",
            f"data = segment_loader(program, {upload['segment_id']!r})",
            f"sequence.write_segment({upload['hardware_slot']!r})",
            f"sequence.segment_size({upload['samples_per_channel']!r})",
            "transfer = sdk.DataTransfer(card)",
            f"transfer.allocate_buffer({upload['samples_per_channel']!r})",
            "transfer.buffer[:] = data.T",
            "transfer.start_buffer_transfer(",
            "    sdk.M2CMD_DATA_STARTDMA, sdk.M2CMD_DATA_WAITDMA)",
            "",
        ]
    lines.append("# 2. Static step table: no per-gate external trigger wait.")
    for step in steps:
        lines += [
            f"sequence.step_memory({step['step_index']!r},",
            f"    next_step_index={step['next_step_index']!r}, "
            f"segment_index={step['segment_index']!r},",
            f"    loops={step['loops']!r}, flags=sdk.{step['flags']})",
        ]
    lines += [
        "sequence.start_step(0)  # Select entry step; does NOT start RF playback.",
        "card.write_setup()",
        "# 3. Runner arms only AFTER every participating card has been uploaded.",
    ]
    if program.get("kind") == "spectrum_bank":
        lines += [
            "# Before each shot, select its finite entry without uploading again:",
            *(
                f"# point {entry['point']}: adapter.select_entry({entry['entry_step']})"
                for entry in program["entries"]
            ),
        ]
    return {
        "configuration": configuration,
        "uploads": uploads,
        "step_writes": steps,
        "upload_listing": "\n".join(lines) + "\n",
    }


_METHODS = (
    ("SpcmAdapter", "configure_for_calibration"),
    ("SpectrumRunner", "run_point"),
    ("SpectrumRunner", "run_resident_point"),
    ("SpectrumRunner", "_load_bank"),
    ("SpcmAdapter", "configure"),
    ("SpcmAdapter", "upload"),
    ("SpcmAdapter", "select_entry"),
    ("SpcmAdapter", "arm"),
    ("SpcmAdapter", "ready"),
    ("SpcmAdapter", "wait_complete"),
)


def export_execution_sources(output):
    """Copy exact runtime bytes and record source ranges without importing it."""
    directory = output / "runtime"
    directory.mkdir(exist_ok=True)
    snapshots = {}
    for entry in RUNTIME_SOURCES:
        content = runtime_source_bytes(entry)
        (output / entry.report_path).write_bytes(content)
        snapshots[entry.key] = content
    runtime = snapshots["spectrum_runtime"]
    tree = ast.parse(runtime.decode("utf-8"))
    methods = {}
    for class_name, method_name in _METHODS:
        cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
        )
        method = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == method_name
        )
        methods[f"{class_name}.{method_name}"] = {
            "start_line": method.lineno,
            "end_line": method.end_lineno,
        }
    sources = {}
    for key, relative in (
        ("spectrum_runtime", "runtime/spectrum_runtime.py"),
        ("host_bridge", "export/experiment.py"),
        ("artiq_kernel", "export/artiq/experiment.py"),
    ):
        content = (output / relative).read_bytes()
        sources[key] = {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    for entry in RUNTIME_SOURCES:
        if entry.key == "spectrum_runtime":
            continue
        content = snapshots[entry.key]
        sources[entry.key] = {
            "path": entry.report_path,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return {
        "status": "code_preview_not_execution",
        "sources": sources,
        "runtime_methods": methods,
    }


def _source_html(execution, output, key, method=None):
    record = execution["sources"][key]
    source = (output / record["path"]).read_text(encoding="utf-8")
    label = record["path"]
    if method:
        span = execution["runtime_methods"][method]
        source = "\n".join(
            source.splitlines()[span["start_line"] - 1 : span["end_line"]]
        )
        label += f" · lines {span['start_line']}–{span['end_line']}"
    return (
        f'<p class="muted"><a href="{record["path"]}">{html.escape(label)}</a>'
        f"<br>Source SHA-256: <code>{record['sha256']}</code></p>"
        f'<pre class="source"><code>{html.escape(source)}</code></pre>'
    )


def execution_html(execution, output):
    """One-page call order, actual shared adapter code, and exported kernels."""

    def method_block(title, *names, opened=False):
        return (
            f"<details {'open' if opened else ''}><summary>{title}</summary>"
            + "".join(
                _source_html(execution, output, "spectrum_runtime", name)
                for name in names
            )
            + "</details>"
        )

    return "\n".join(
        [
            '<section id="execution"><h2>ARTIQ + Spectrum · 完整执行流程</h2>',
            "<p>以下是导出时的真实运行适配器源码快照，"
            "以及本实验生成的 host / kernel 源码。"
            "本页没有调用仪器；具体卡号、配置与逐点 SDK 参数见下面的编译结果。</p>",
            "<h3>运行前：时钟初始化与重新校准</h3>",
            "<p><code>configure_for_calibration()</code> 显式设置外部参考时钟、采样率，"
            "并更新 clock session，使旧相位校准失效。它不在正常 shot 调用链里。"
            "完成新的实测校准后才能进入硬件运行；"
            "示例 SIM 序列号和未测校准不能直接运行实机。</p>",
            method_block(
                "时钟设置源码 · 显式初始化，不是每 shot 自动执行",
                "SpcmAdapter.configure_for_calibration",
            ),
            "<p>正式运行由导出的 <code>GeneratedExperiment.run()</code> 进入："
            "先检查 ARTIQ 目标、调用一次 <code>initialize_devices()</code> "
            "初始化 kernel，"
            "再按 host_execution_plan 中的 averaging / chunk 顺序进入 shot 循环。"
            "这次设备初始化与每 shot 的点 kernel 分开；完整代码见下方。</p>",
            "<h3>每个有 Spectrum 工作的 shot</h3>",
            '<ol class="execution-flow">'
            "<li><b>Host 准备</b>"
            "<code>result_adapter.before_shot(point, shot)</code></li>"
            "<li><b>全部卡预检</b>先 probe 全部卡，"
            "检查身份、容量、时钟与有效校准。</li>"
            "<li><b>首次进入或更换 bank 时配置及上传</b>"
            "A.configure → A.upload → B.configure → B.upload。"
            "常规 configure 核对时钟，设置输出和 EXT0；"
            "upload 写共享样本及完整 step table。bank 仍驻留时跳过 DMA，"
            "每个 shot 只选择对应点的有限入口。</li>"
            "<li><b>全部卡 arm</b>A.arm → B.arm → 等待全部 ready。"
            "此时尚未启动 AWG 序列，DDS 囚禁输出已保持。</li>"
            '<li class="kernel-step"><b>一个 ARTIQ 点 kernel</b>'
            "Host 回调 <code>_execute_point(point)</code> 分派到对应的 "
            "<code>point_N()</code> kernel：执行块外准备 → "
            "按时间表同步发各卡启动 TTL → "
            "Spectrum 自主走完静态序列；ARTIQ 按编译时间线执行读出。"
            "块内 gate / delay / virtual Z 没有 host 或逐门 TTL 介入。</li>"
            "<li><b>完成检查</b>kernel 返回后等待所有卡完成，再 "
            "<code>result_adapter.after_shot(point, shot)</code>，归档 receipt。</li>"
            "</ol>",
            "<p>上图 A/B 是双卡示意；每点实际参与卡以 manifest 为准。"
            "schema 2 复用驻留 AWG bank，schema 3 还包含逐 shot DDS 命令；"
            "scope=global 保持原采样顺序，循环平均时可能重新换入此前的 bank；"
            "scope=chunk 完成本块平均后再换块。schema 1 兼容路径仍逐点上传。"
            "不含活动 Spectrum 程序的点直接走 ARTIQ，不 configure / upload / arm，"
            "也不产生自动启动 TTL。"
            "失败时 runner 停止已接触的卡；未全部 ready 就不能进入点 kernel。</p>",
            method_block(
                "Host 协调源码 · 全部 ready 后才调用 ARTIQ",
                "SpectrumRunner.run_point",
                "SpectrumRunner.run_resident_point",
                "SpectrumRunner._load_bank",
            ),
            '<h3 id="spectrum-code">Spectrum 配置、上传与启动源码</h3>',
            "<details><summary>DDS20 持续囚禁：实际命令编译和适配器源码</summary>"
            "<p>DDS 卡只在会话初始化时启动；shot 间和 AWG bank 更换时保持输出。"
            "逐 shot 追加已预装命令、外触发启动、定时执行后回到保持状态。"
            "以下 AWG 的 segment、零尾段和重新 arm 说明不适用于 DDS。"
            "DDS 轨迹图是命令预测，没有软件绝对相位或最终 DAC 样本。</p>",
            _source_html(execution, output, "spectrum_dds_runtime"),
            _source_html(execution, output, "spectrum_dds"),
            "</details>",
            method_block(
                "1 · 输出和 EXT0 触发配置（含每项读回核对）",
                "SpcmAdapter.configure",
            ),
            "<p>启动输入为 EXT0 上升沿；默认阈值 1000 mV、50 Ω、DC coupling。"
            "实际值及来源逐卡列出。SDK <code>trigger.delay(0)</code> 是附加可编程延迟；"
            "编译契约的 <code>trigger_latency_samples</code> "
            "是触发到 sample 0 的延迟模型，二者不同。</p>",
            method_block(
                "2 · 波形 DMA 上传 → 3 · step table 写入", "SpcmAdapter.upload"
            ),
            "<p>样本来自 manifest recipe，经 SegmentCache 生成/校验后 DMA；"
            "<code>data.T</code> 匹配 SDK buffer 的通道维度。下方 .bin 是核查副本。"
            "硬件 segment slot 按 segments 列表次序编号，不直接使用逻辑 segment ID。"
            "<code>SPCSEQ_ENDLOOPALWAYS</code> 表示完成有限循环后继续，末步 "
            "<code>SPCSEQ_END</code> 结束；门间不等待外触发。"
            "<code>start_step</code> 只选择入口。bank 中的每个点独立结束，"
            "不会自动跳入下一个扫描点。</p>",
            method_block("逐 shot 入口选择 · 无重复 DMA", "SpcmAdapter.select_entry"),
            method_block(
                "4 · arm → ready → 完成检查",
                "SpcmAdapter.arm",
                "SpcmAdapter.ready",
                "SpcmAdapter.wait_complete",
            ),
            "<p><code>arm()</code> 使用 <code>M2CMD_CARD_ENABLETRIGGER</code>，"
            "等待外部 TTL，没有 FORCE TRIGGER。"
            "这里 SDK 的 <code>M2STAT_CARD_READY</code> 表示已停止/完成；"
            "不能把该状态位当成“已 arm 等触发”。请以 ready() 源码判据为准。</p>",
            '<h3 id="artiq-code">本实验的 host bridge 与 ARTIQ kernel</h3>',
            "<details><summary>完整 host bridge · 接入 SpectrumRunner</summary>",
            _source_html(execution, output, "host_bridge"),
            "</details><details><summary>完整 ARTIQ 导出代码 · "
            "含各扫描点的触发时序</summary>",
            _source_html(execution, output, "artiq_kernel"),
            "</details></section>",
        ]
    )
