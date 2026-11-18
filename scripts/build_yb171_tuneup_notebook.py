"""Build the offline, plain-DSL 171Yb tune-up laboratory notebook."""

# Long Chinese explanations and Markdown tables are intentional.
# ruff: noqa: E501
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "examples/artiq_spectrum/ramsey_lab"
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source, *, tags=()):
    cells.append(
        nbformat.v4.new_code_cell(
            textwrap.dedent(source).strip(),
            metadata={"tags": list(tags)} if tags else {},
        )
    )


md(r"""
# ¹⁷¹Yb 核自旋 qubit tune-up · Global RF + interaction-zone clock / Rydberg

**实验路线与配置。** 逻辑编码为 $|0\rangle=|{}^1S_0,m_F=-1/2\rangle$、$|1\rangle=|{}^1S_0,m_F=+1/2\rangle$；约定选定 clock 分支只把 $|1\rangle$ 映到 $|c\rangle\in{}^3P_0$，再驱动 $|c\rangle\leftrightarrow|r\rangle$。具体 Zeeman 分支、偏振、磁场必须按实验室确认，不能把这个编码约定当成实际接线证明。

这是一份可按顺序 **Run All 的离线实验工作本**：完整配置、命名 pulse、plain DSL 实验函数、扫描与采数建议、可运行分析示范、代表序列编译，以及校准记录。默认不连接仪器；图和拟合均显式标明 synthetic/model。真实制备、搬运、分类读出需要替换 native 回调。

上手路线沿用 Ramsey：**device_db / spec_db → descriptor → LabConfig 与 Qubit → native ARTIQ → 实验函数 → compile / visualization → 显式连接实机**。前两节把配置完整写出；之后每个实验直接调用 `lab.compile(...)`，不需要理解编译器内部目录。实验顺序保持如下，分析和拟合只用于解释物理结果。

两比特主线为 **回波调纠缠相位 → Bell/宇称定共同相位 → 回波随机诊断 → 无回波 SSB**；各节给出论文出处与可运行的数学核验。入门参考本项目 [Plain Ramsey 教程](ramsey_clock_plain.ipynb)，组件扩展见 [开发指南](../development.md)。

| 顺序 | 实验族 | 得到什么 |
|---|---|---|
| 0 | 装载/冷却、B 场、SPAM、AOM、搬运与存活 | 后续分析的有效分母与控制链路 |
| 1 | RF spectroscopy、2D Rabi/Ramsey、echo/CPMG/XY8、人口保持 | RF 共振、π/π2、相干与 loss 区分 |
| 2 | 相位轴、ALLXY、幅度×重复数误差放大、global RF 1Q RB | 系统误差和每 site 单比特性能 |
| 3 | clock 2D chevron/Ramsey、shelving phase/hold、light shift、optical RB | shelving 返回、相干、串扰与泄漏 |
| 4 | Rydberg 2D chevron、返回保持、真实单占据/pair 对照要求 | 失谐、collective Rabi、blockade 与回收 |
| 5 | CZ closure/echo 2D、Bell、输入态损失、GERB-style、SSB 与波形候选 | 对当前全局控制可辨识的纠缠门指标与优化流程 |
| 6 | 原始 shot、误差条、校准变更与 daily check | 可重复的调参闭环 |

启动：项目根目录 `uv sync --locked --group notebooks`，然后 `uv run --locked --group notebooks jupyter lab docs/tutorials/yb171_global_rf_tuneup.ipynb`，使用项目 CPython 3.12 kernel。
""")
md(r"""
## 1. 控制能力与不能偷加的能力

- **RF 全局**：一次 RF 波形作用所有处于 $^1S_0$ 的已登记原子，包括 storage spectator。q0 只是波形校准来源，不是寻址选择器。逐 site 记录不同转角、频率、对比度；不能凭一个平均值声称每个 site 已校准。
- **clock/Rydberg 是 zone-wide**：搬入 interaction zone 的原子共同受光，区外作为 spectator；zone 内不假设逐原子光寻址。搬运本身不产生局域 RF，不能据此写“固定控制位、只 Ramsey 旋转目标位”的实验。
- 本工作本每个 shot 使用固定成员 q0/q1 的 zone，加一个只映射 RF 的 `spectator`，以聚焦调参序列。需要动态搬运时使用当前 DSL 的 `IZ`、`TransportConfig` 和 `move`，参见 [Clock Ramsey 搬运教程](ramsey_clock_transport.ipynb)；编译器按实际位置推导 zone 占用。仅修改 `targets` 或 `position` 不会发出搬运指令。
- 不把同一 zone 内两原子的独立 X/Y 门写进 full 2Q Clifford RB。默认两比特统计主要覆盖对称子空间；噪声破坏对称性仍可能被检出，但不等于完成整个四维空间的门表征。
- 每次 **完整基态 CZ** 都是 clock π 上行 → Rydberg 门 → clock π 下行，包含空隙与共同相位补偿。只 shelf 一次后连打 N 个 UV 门不是 N 个本工作本定义的基态 CZ。

实验前需先检查：磁场方向/偏振选择性、zone 内 Ω/AC-Stark 均匀性、原子间距与不同 pair 间串扰、AOM 消光/相位跳变/声传播延迟、光学相位参考、陷阱开关的加热和 recapture、相机 site 配准。对 storage spectator 做 RF Ramsey 与有/无 zone 操作对照；只 shelf 一个核自旋分量不能保护任意未知 spectator qubit。
""")
code(
    """
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit
from taiyidsl import (
    DeviceSetup, Experiment, Hz, LabConfig, NativeKernelPackage, Qubit,
    Square, SweepParameter, deg, delay, kHz, ms, ns, play, rad, reset_phase, us,
)
""",
    tags=("rb-imports",),
)
code("""
from IPython.display import Image, Markdown, display

ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src/taiyidsl").is_dir())
OUTPUT = ROOT / "output/yb171_tuneup"
OUTPUT.mkdir(parents=True, exist_ok=True)
RNG = np.random.default_rng(171)
SHOTS_PREVIEW = 2

""")
md("""
## 2. device_db / spec_db → descriptor → LabConfig → Qubit

沿用双 Spectrum 卡配置：A0=RF，B0=clock AOM drive，B1=Rydberg AOM drive。1.25 GHz 是该后端支持的采样率，不是光学频率或 Rabi rate。下列地址、RTIO channel、延迟、幅度全是离线占位。实际 device_db 仍以实验室 MOT 库为准，不覆盖它。

`device_db` 保存 ARTIQ 设备事实，`spec_db` 保存卡配置；`descriptor` 只描述谁连接谁。`DeviceSetup` 读取它们，`LabConfig` 作为后续编译与连接的入口。先看接线，再定义 Qubit；构造 `lab` 时登记所有受全局 RF 影响的原子。

`sharing="global"` 是 DSL 的“共享同一物理输出”标志；clock/Rydberg 在此只共享给 zone 成员，**并不表示照亮全阵列**。
""")
code((LAB / "device_db.py").read_text(encoding="utf-8"))
code((LAB / "spec_db.py").read_text(encoding="utf-8"))
code("""
descriptor = {
    "name": "yb171_zone_tuneup", "backend": "artiq-spectrum",
    "instruments": {
        "core": {"kind": "artiq", "device": "core"},
        "A": {"kind": "spectrum", "device": "A"},
        "B": {"kind": "spectrum", "device": "B"},
    },
    "channels": {
        "nuclear_rf": {"instrument": "A", "channel": 0, "sharing": "global"},
        "clock_zone": {"instrument": "B", "channel": 0, "sharing": "global"},
        "rydberg_zone": {"instrument": "B", "channel": 1, "sharing": "global"},
        "camera": {"instrument": "core", "device": "ttl_Camera_Trig"},
        "awg_trigger_a": {"instrument": "core", "device": "awg_trigger_a"},
        "awg_trigger_b": {"instrument": "core", "device": "awg_trigger_b"},
    },
    "sync_groups": {"qubit_awg": {
        "cards": ["A", "B"],
        "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b"},
    }},
    "signals": {
        "q0": {"rf": "nuclear_rf", "clock": "clock_zone", "rydberg": "rydberg_zone"},
        "q1": {"rf": "nuclear_rf", "clock": "clock_zone", "rydberg": "rydberg_zone"},
        "spectator": {"rf": "nuclear_rf"},
        "camera": "camera",
    },
}
setup = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
display(setup.connections())
""")
md("""
### 2.1 Qubit：角色参数与实验室入口

下面用同一组示例参数定义 q0、q1 和 spectator。每个 Qubit 会保存自己的参数副本，不需要额外的 Python 深拷贝。`signals` 使用明确的角色名；descriptor 决定它们是否共用同一物理输出。
""")
code("""
parameters = {
    "rf": {"frequency": 30*kHz, "pi_duration": 200*us, "pi2_duration": 100*us,
           "pi_amplitude": .2, "pi2_amplitude": .2, "waveform": "square", "frame": "spin"},
    "clock": {"frequency": 200*kHz, "pi_duration": 100*us, "pi2_duration": 50*us,
              "pi_amplitude": .2, "pi2_amplitude": .2, "waveform": "square", "frame": "clock_control"},
    "rydberg": {"frequency": 500*kHz, "pi_duration": 2*us, "pi2_duration": 1*us,
                "pi_amplitude": .2, "pi2_amplitude": .2, "waveform": "square", "frame": "rydberg_control"},
    "extensions": {"calibration": {"status": "illustrative_uncalibrated"}},
}
q0 = Qubit(uid="q0", signals={"rf": "q0.rf", "clock": "q0.clock", "rydberg": "q0.rydberg"}, parameters=parameters)
q1 = Qubit(uid="q1", signals={"rf": "q1.rf", "clock": "q1.clock", "rydberg": "q1.rydberg"}, parameters=parameters)
spectator = Qubit(uid="spectator", signals={"rf": "spectator.rf"}, parameters={"rf": parameters["rf"]})
lab = LabConfig(setup=setup, qubits=[q0, q1, spectator], emulation=True)
""")
md(r"""
### 2.2 AOM 频率与相位映射必须实测

对每条光路定义 $\delta_{opt}=s_f(f_{AWG}-f_{AWG,0})$、$\phi_{opt}=s_\phi\phi_{AWG}+\phi_0$。$s_f$ 可能包含正负衍射级与 double pass 因子，$s_\phi$ 也要由干涉/原子 Ramsey 测量确认。下面默认 +1 只为演示，不代表实际光路。

`frequency` 保存电驱动载波。200 kHz/500 kHz 是方便离线查看的 IF 示例；不能解释为 578 nm clock 或 UV 光学频率。扫描失谐先转为最终电频率，再传给 DSL；`SweepParameter` 不能直接加常数或除以 2。
""")
code("""
optical_mapping = {"clock": {"frequency_scale": 1., "phase_scale": 1.},
                   "rydberg": {"frequency_scale": 1., "phase_scale": 1.}}

def optical_frequency_axis(q, role, detunings_hz):
    base = getattr(q.parameters, role).frequency
    scale = optical_mapping[role]["frequency_scale"]
    return SweepParameter(role + "_drive_hz", [base + float(d)/scale for d in detunings_hz], "Hz")

# 命名pulse保存幅度与时间；frequency不写时从当前Experiment快照读取。
rf_pi = Square(duration=q0.parameters.rf.pi_duration, amplitude=q0.parameters.rf.pi_amplitude)
rf_pi2 = Square(duration=q0.parameters.rf.pi2_duration, amplitude=q0.parameters.rf.pi2_amplitude)
clock_pi = Square(duration=q0.parameters.clock.pi_duration, amplitude=q0.parameters.clock.pi_amplitude)
clock_pi2 = Square(duration=q0.parameters.clock.pi2_duration, amplitude=q0.parameters.clock.pi2_amplitude)
rydberg_pi = Square(duration=q0.parameters.rydberg.pi_duration, amplitude=q0.parameters.rydberg.pi_amplitude)
""")
md("""
### 2.3 native ARTIQ：准备与读出接口：不把 TTL 动作当测量

下面只复用现有 GreenMOT **TTL fixture**，它不制备、搬运或读出原子。实际 `prepare` 需完成装载/冷却/光抽运到 00、选择固定 zone 成员、搬运/陷阱切换、等待实测稳定时间。实际 `readout` 需提供 site-resolved 0/1、clock 残留、loss 与 invalid 分类。

native 回调在 realtime 的准备/读出位置；未声明确定时长的 native 操作不能塞进 AWG sequence。受支持的 DDS 搬运可使用独立配置的 `move`。相机 trigger 由 native readout 独占，不再额外 acquire 重复触发。无图像适配器时编译不会产生测量数据，实际取帧见 [读出 adapter 指南](../readout_adapters.md)。

这里保留 `NativeKernelPackage.from_package`：现有 fixture 初始化会 `core.reset()`，它属于旧式整包生命周期，清理方法每 shot 执行。新建独立 MOT、冷却和相机模块推荐 `NativeModule.from_package` + `NativeProgram`，由会话统一负责 core reset；组件 `finalize` 是会话结束钩子，不能直接替换旧包的每 shot 清理。见 [原生组件指南](../native_components.md) 和 [实验室扩展示例](../../examples/lab_extension/README.md)。两条路径各有适用边界，不因接口较新而静默改变旧实验的设备状态。
""")
code("""
package = NativeKernelPackage.from_package(
    root=ROOT / "examples/artiq_spectrum/greenmot_native",
    entry="greenmot_fixture:GreenMOTFixture",
    resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
    initialize_method="initialize_native", finalize_method="finalize_native",
)
prepare_prefix = package.kernel("prepare_MOT", timing="prefix")
prepare_tail = package.kernel("finish_preparation", timing="timed", duration=15*ms+16*ns,
                              end_boundary="OFFLINE fixture TTL boundary; no atoms or transport")
readout_fixture = package.kernel("execute_readout", timing="suffix", duration=100.48*ms)

def prepare_fixture():
    prepare_prefix()
    prepare_tail()

callbacks = {"prepare": prepare_fixture, "readout": readout_fixture, "shots": SHOTS_PREVIEW}

""")
md(r"""
## 3. 先建立 SPAM / loss / leakage 分母

必须分开：装载前不存在、装载后丢失、探测无效、存活但在 $c/r$ 或其他态、有效逻辑 0/1。dark 不自动等于逻辑 1。若不能逐 shot 识别 clock 残留，用独立 repump/return 检测对照估计，并记录它与主测量并非同一次 shot。

分别标定准备 0、准备 1、无 pushout、无原子、clock shelved、repump on/off；取得逐 site 混淆矩阵（行观测、列真实），并检查双原子联合误判相关性。先报告 raw counts，再用约束 likelihood 校正；简单矩阵逆可产生负概率。不能用同一套未经校准的 SPAM 同时证明 SPAM 与门保真度。

下面的 **synthetic counts** 示范分母。实际联合 $P_{00}$ 必须来自同一 shot 的 pair，不可用 $P_0(q0)P_0(q1)$ 代替。
""")
code("""
def summarize_pair_counts(counts):
    required = {"00", "01", "10", "11", "leak", "loss", "invalid"}
    if set(counts) != required or any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError("Use exclusive, nonnegative integer outcome counts")
    total = sum(counts.values())
    valid = total - counts["invalid"]
    logical = sum(counts[k] for k in ("00", "01", "10", "11"))
    if total == 0 or valid == 0:
        raise ValueError("No valid acquired trials")
    return {"attempts": total, "valid_trials": valid,
            "invalid_fraction": counts["invalid"]/total,
            "atom_survival": (valid-counts["loss"])/valid,
            "logical_survival": logical/valid,
            "P00_unconditional": counts["00"]/valid,
            "P00_given_logical": counts["00"]/logical if logical else None,
            "leakage_fraction": counts["leak"]/valid}

synthetic_counts = {"00": 850, "01": 20, "10": 15, "11": 30, "leak": 25, "loss": 50, "invalid": 10}
display([summarize_pair_counts(synthetic_counts)])
assert summarize_pair_counts({"00":0,"01":0,"10":0,"11":0,"leak":1,"loss":1,"invalid":0})["P00_given_logical"] is None
""")
md(r"""
## 4. RF qubit spectroscopy

从统一 0 态出发，扫最终 RF 频率，固定弱/长探测脉冲。先粗扫定位，再减小步长（小于线宽的约 1/5）；若强驱动则使用受驱两能级线型而非盲拟合 Lorentzian。

按 site 保存共振和展宽，观察磁场梯度；选全局工作频率时看最差 site 与分布，不能对每个原子分别使用不同 RF 频率。重复 B 场/陷阱深度扫描可分离 Zeeman 和光移。
""")
code("""
rf_frequency = SweepParameter("rf_frequency", [28000, 30000, 32000], "Hz")
rf_probe = Square(duration=400*us, amplitude=.05, frequency=rf_frequency)

def rf_spectroscopy(q, frequency_axis, probe, *, prepare, readout, shots=2):
    exp = Experiment("rf_spectroscopy", [q], metadata={"data": "not_acquired"})
    with exp.average(shots), exp.sweep(frequency_axis), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, probe)
        readout()
    return exp

rf_spec_exp = rf_spectroscopy(q0, rf_frequency, rf_probe, **callbacks)
rf_spec_preview = lab.compile(rf_spec_exp)
display(rf_spec_preview.axes)
""")
md(r"""
## 5. RF length Rabi 与 π/π2 校准

固定载波与功率，扫时长；粗扫覆盖至少两三个周期，精扫 π/2、π 附近。拟合包含 offset、contrast、衰减和必要的失谐；零时长背景用独立 reference shot，不构造零长 Square。

π/2 和 π 校准分别存入 `pi2_duration/pi2_amplitude` 与 `pi_duration/pi_amplitude`。只在理想恒定 Ω 下才有一半时长关系。AOM 不参与 RF 旋转，但 RF 开关/放大器起振与 ringing 同样需要检查。
""")
code("""
rf_lengths = SweepParameter("rf_length", [50, 100, 200, 300], "us")
rf_rabi_pulse = Square(duration=rf_lengths, amplitude=q0.parameters.rf.pi_amplitude)

def rf_rabi(q, lengths, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_length_rabi", [q])
    with exp.average(shots), exp.sweep(lengths), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pulse)
        readout()
    return exp

rf_rabi_preview = lab.compile(rf_rabi(q0, rf_lengths, rf_rabi_pulse, **callbacks))

def damped_rabi(t, offset, amplitude, omega, decay):
    return offset - amplitude*np.exp(-t/decay)*np.cos(omega*t)

# SYNTHETIC data only; no automatic calibration update.
times = np.linspace(0, 1000e-6, 81)
p_true = damped_rabi(times, .5, .47, 2*np.pi*2500, .004)
p_demo = RNG.binomial(800, p_true)/800
fit_rabi, _ = curve_fit(damped_rabi, times, p_demo, p0=[.5,.45,2*np.pi*2400,.004],
                       bounds=([.3,0,2*np.pi*1000,1e-5],[.7,.5,2*np.pi*5000,.1]))
fig, ax = plt.subplots(figsize=(7,3))
ax.plot(times*1e6, p_demo, ".", label="Synthetic shots")
ax.plot(times*1e6, damped_rabi(times,*fit_rabi), label="Fit")
ax.set(xlabel="RF pulse duration (us)", ylabel="P1", title="SYNTHETIC RF Rabi")
ax.legend()
fig.savefig(OUTPUT / "synthetic_rf_rabi.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "synthetic_rf_rabi.png")))
print("Synthetic pi duration (us):", np.pi/fit_rabi[2]*1e6)
""")
md(r"""
### 5.1 二维 Rabi：frequency × duration 与 amplitude × duration

第一张 chevron 图同时扫共振两侧和脉冲时长，用来找共振与 π 时间；第二张图固定频率，检查幅度到 Rabi 频率的实测关系。DAC amplitude 不是光功率或 Ω，不能预设线性。
下面用 3×3 小网格离线编译。**外轴为行、内轴为列，内轴最快**；真实采数按 shot 的坐标和有效分类聚合，缺帧留空，不能把不完整数据直接 reshape 成完整图。
失谐列表先换成最终正电频率，再传 `frequency=`；`detuning=` 不接受扫描轴。每点都重新 prepare/readout，只有物理脉冲进入 triggered sequence。
""")
code("""
rf_chevron_frequency = SweepParameter("rf_chevron_hz", [q0.parameters.rf.frequency+d for d in (-1000,0,1000)], "Hz")
rf_chevron_time = SweepParameter("rf_chevron_time", [100,200,300], "us")
rf_amplitudes = SweepParameter("rf_amplitude", [.16,.20,.24], "dimensionless")
rf_chevron_pulse = Square(duration=rf_chevron_time, amplitude=q0.parameters.rf.pi_amplitude)
rf_amplitude_pulse = Square(duration=rf_chevron_time, amplitude=rf_amplitudes)

def rf_chevron(q, frequency, duration, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_chevron_2d", [q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pulse, frequency=frequency)
        readout()
    return exp

def rf_amplitude_rabi(q, amplitude, duration, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_amplitude_rabi_2d", [q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pulse)
        readout()
    return exp

rf_chevron_preview = lab.compile(rf_chevron(q0,rf_chevron_frequency,rf_chevron_time,rf_chevron_pulse,**callbacks))
rf_amplitude_preview = lab.compile(rf_amplitude_rabi(q0,rf_amplitudes,rf_chevron_time,rf_amplitude_pulse,**callbacks))
""")
md(r"""
## 6. Ramsey、echo、CPMG 和频率锁定

Ramsey 只扫暗时间，显式使用 `frequency=rf.frequency+detuning`。不要再手加 $2\pi\delta\tau$ 到第二 RF，相同 carrier frame 已承载该演化。入口 reset phase 不会重置原子量子态；序列中不重复 reset。

先用有意 detuning 得到多周期条纹，校准频率，再分别取第二脉冲 ±90° 的 quadrature。两条频率探针交错采数可形成 host-side lock；锁定器需有有效 shot 数、对比度和最大步长门限，校准更新后重新构建实验，不假设现有例子已接实时反馈。

Echo 扫 `half_tau`，真实暗时间为两倍；改变 π 相位以分离误差。CPMG 取首末 τ/2、中间 τ，π 脉冲绕 Y；记录总暗时间与包含有限脉冲的总时间。不要把一个短预览扫描的拟合称为实测 T2。
""")
code("""
def rf_ramsey(q, tau, pi2_pulse, *, detuning=1*kHz, phase=0*deg, prepare, readout, shots=2):
    frequency = q.parameters.rf.frequency*Hz + detuning
    exp = Experiment("rf_ramsey", [q])
    with exp.average(shots), exp.sweep(tau), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pi2_pulse, frequency=frequency)
            delay(q.rf, tau)
            play(q.rf, pi2_pulse, frequency=frequency, phase=phase)
        readout()
    return exp

def rf_echo(q, half_tau, pi2_pulse, pi_pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_hahn_echo", [q])
    with exp.average(shots), exp.sweep(half_tau), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pi2_pulse)
            delay(q.rf, half_tau)
            play(q.rf, pi_pulse, phase=90*deg)
            delay(q.rf, half_tau)
            play(q.rf, pi2_pulse, phase=180*deg)
        readout()
    return exp

def rf_cpmg(q, pi2_pulse, pi_pulse, *, tau, echoes, prepare, readout, shots=2):
    if type(echoes) is not int or echoes < 1:
        raise ValueError("echoes must be a positive integer")
    exp = Experiment("rf_cpmg", [q], metadata={"echoes": echoes, "dark_time_s": float(tau)*echoes})
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf, pi2_pulse)
            delay(q.rf, tau/2)
            for i in range(echoes):
                play(q.rf, pi_pulse, phase=90*deg)
                delay(q.rf, tau/2 if i == echoes-1 else tau)
            play(q.rf, pi2_pulse, phase=180*deg)
        readout()
    return exp

tau = SweepParameter("tau", [10,100,1000], "us")
half_tau = SweepParameter("half_tau", [10,100,500], "us")
ramsey_preview = lab.compile(rf_ramsey(q0,tau,rf_pi2,**callbacks))
ramsey_preview.export_report(OUTPUT / "rf_ramsey_report")
echo_preview = lab.compile(rf_echo(q0,half_tau,rf_pi2,rf_pi,**callbacks))
cpmg_preview = lab.compile(rf_cpmg(q0,rf_pi2,rf_pi,tau=100*us,echoes=2,**callbacks))
""")
md(r"""
### 6.1 二维 Ramsey：失谐／相位 × dark time

频率图用于定位共振和漂移；相位图在每个暗时间拟合 fringe 的 offset、contrast 与 phase，避免把相位漂移误认作 T2 衰减。
两次半 π 使用同一频率，只在序列开头 reset phase；**不再把 2πδτ 加到第二次 pulse 相位**。有限失谐也会改变准备和分析脉冲，下面的模型图保留有限 pulse 宽度。
相位扫描是独立实验：固定 RF 频率，用末 pulse 的分析相位读两个 quadrature。扫描时间是 pulse 边缘之间的 dark time。
""")
code("""
rf_ramsey_frequency = SweepParameter("rf_ramsey_hz", [q0.parameters.rf.frequency+d for d in (-1000,0,1000)], "Hz")
rf_dark_times = SweepParameter("rf_dark_time", [10,100,500], "us")
rf_analysis_phases = SweepParameter("rf_analysis_phase", [0,90,180,270], "deg")

def rf_ramsey_map(q, frequency, dark_time, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_ramsey_frequency_time", [q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(dark_time), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,pulse,frequency=frequency)
            delay(q.rf,dark_time)
            play(q.rf,pulse,frequency=frequency)
        readout()
    return exp

def rf_ramsey_phase_map(q, phase, dark_time, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_ramsey_phase_time", [q])
    with exp.average(shots), exp.sweep(phase), exp.sweep(dark_time), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,pulse)
            delay(q.rf,dark_time)
            play(q.rf,pulse,phase=phase)
        readout()
    return exp

rf_ramsey_map_preview = lab.compile(rf_ramsey_map(q0,rf_ramsey_frequency,rf_dark_times,rf_pi2,**callbacks))
rf_ramsey_phase_preview = lab.compile(rf_ramsey_phase_map(q0,rf_analysis_phases,rf_dark_times,rf_pi2,**callbacks))
""")
md(r"""
### 6.2 XY8 与人口保持：分开看相干、弛豫和原子丢失

XY8 的 π 轴依次为 X Y X Y Y X Y X；首尾各 half_gap，中间两份 half_gap。N 个 π pulse 对应暗时间 2N·half_gap，总时间还含 N 个 π 和两个 π/2。
分别准备两个正交相干方向，再与 Hahn/CPMG 比较；下面变量名 x/y 指首个半 π 的旋转轴，分别从 +Z 准备 −Y/+X。单轴序列保护某一态不代表任意态同样受保护。[Tyryshkin et al.](https://arxiv.org/abs/1011.1903)讨论了这种初态和 pulse 误差依赖。

人口保持用 0/1 两种初态、完全等时的前置槽，逐 site 记录 P0/P1/loss/invalid。两种初态共同约束平衡人口和跃迁率；survival 衰减不是自旋 T1。示例只用短等待展示结构，实际时间窗需根据相干和漂移扩展。
""")
code("""
xy8_half_gaps = SweepParameter("xy8_half_gap", [10,50,100], "us")
population_holds = SweepParameter("population_hold", [10,100,1000], "us")

def rf_xy8(q, half_gap, half_pulse, pi_pulse, *, blocks=1, initial_phase=0*deg, prepare, readout, shots=2):
    if type(blocks) is not int or blocks < 1:
        raise ValueError("blocks must be a positive integer")
    phases = [0,90,0,90,90,0,90,0]*blocks
    exp = Experiment("rf_xy8", [q], metadata={"pi_count":len(phases),"initial_phase_rad":float(initial_phase)})
    with exp.average(shots), exp.sweep(half_gap), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,half_pulse,phase=initial_phase)
            delay(q.rf,half_gap)
            for i,phase in enumerate(phases):
                play(q.rf,pi_pulse,phase=phase*deg)
                delay(q.rf,half_gap)
                if i+1 < len(phases):
                    delay(q.rf,half_gap)
            play(q.rf,half_pulse,phase=initial_phase+180*deg)
        readout()
    return exp

def rf_population_hold(q, hold, pi_pulse, *, prepare_one, prepare, readout, shots=2):
    exp = Experiment(f"rf_population_hold_{int(prepare_one)}", [q])
    with exp.average(shots), exp.sweep(hold), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            if prepare_one:
                play(q.rf,pi_pulse)
            else:
                delay(q.rf,pi_pulse.duration)
            delay(q.rf,hold)
        readout()
    return exp

xy8_x_preview = lab.compile(rf_xy8(q0,xy8_half_gaps,rf_pi2,rf_pi,**callbacks))
xy8_y_preview = lab.compile(rf_xy8(q0,xy8_half_gaps,rf_pi2,rf_pi,initial_phase=90*deg,**callbacks))
population_zero_preview = lab.compile(rf_population_hold(q0,population_holds,rf_pi,prepare_one=False,**callbacks))
population_one_preview = lab.compile(rf_population_hold(q0,population_holds,rf_pi,prepare_one=True,**callbacks))
""")
md(r"""
### 6.3 Spin lock：lock amplitude × duration

Yπ/2 把 Bloch vector 放到 +X，连续 X drive 锁定，−Yπ/2 返回读出。锁定频率使用已校准共振，两个参考半 π 不随 lock 幅度变动。
按 lock 幅度比较旋转坐标系的保持曲线，同时记录 loss；只在驱动失谐、pulse 误差和读出漂移受控后才解释 T1ρ。完整噪声谱反演还需响应函数、滤波带宽和噪声模型，当前不伪造该结果。
""")
code("""
lock_amplitudes = SweepParameter("lock_amplitude", [.08,.12,.16], "dimensionless")
lock_durations = SweepParameter("lock_duration", [100,500,1000], "us")
rf_lock_pulse = Square(duration=lock_durations,amplitude=lock_amplitudes)

def rf_spin_lock(q, amplitude, duration, half_pulse, lock_pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_spin_lock_2d",[q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,half_pulse,phase=90*deg)
            play(q.rf,lock_pulse)
            play(q.rf,half_pulse,phase=270*deg)
        readout()
    return exp

spin_lock_preview = lab.compile(rf_spin_lock(q0,lock_amplitudes,lock_durations,rf_pi2,rf_lock_pulse,**callbacks))
""")
md(r"""
## 7. RF 轴相位、ALLXY 与误差放大

先用 Xπ/2–Rφπ/2 扫描验证 X/Y 相差 90°、负轴相差 180°；该扫描不能单独区分所有 detuning 与 phase offset。对照 Ramsey 并交换脉冲轴。

ALLXY 使用标准 21 对；小写 x/y 是半 π，大写 X/Y 是 π，I 是与半 π 同长 idle。理想 P1 为前 5 个 0、中间 12 个 1/2、最后 4 个 1。它是失谐/面积/轴误差诊断，不直接给 gate fidelity。所有原子收到相同门对，逐 site 比较。

误差放大建议：$(X_\pi)^{2n}$、$(X_{\pi/2})^{4n}$ 及正负轴交替，扫描 n 与幅度偏置。先检查 drift/leakage，避免以较长序列的单一极小值锁错分支。
""")
code("""
# 本段仅加载可审阅的数学分析；后面的实验函数仍直接写出 DSL 时序。
import importlib.util

spec = importlib.util.spec_from_file_location("yb_analysis", ROOT / "examples/yb171_tuneup_analysis.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)
""")
code("""
rf_gate_map = {"I": (None,0), "x":(rf_pi2,0), "y":(rf_pi2,90), "X":(rf_pi,0), "Y":(rf_pi,90),
               "x90":(rf_pi2,0), "xm90":(rf_pi2,180), "y90":(rf_pi2,90), "ym90":(rf_pi2,270),
               "x180":(rf_pi,0), "y180":(rf_pi,90)}

def rf_phase_scan(q, phase, pulse, *, prepare, readout, shots=2):
    exp = Experiment("rf_axis_phase", [q])
    with exp.average(shots), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,pulse)
            play(q.rf,pulse,phase=phase)
        readout()
    return exp

def rf_word_experiment(q, word, gate_map, *, name, prepare, readout, shots=2):
    exp = Experiment(name,[q],metadata={"word":list(word),"control":"same RF on every registered atom"})
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            for gate in word:
                pulse, phase = gate_map[gate]
                if pulse is None:
                    delay(q.rf, q.parameters.rf.pi2_duration)
                else:
                    play(q.rf,pulse,phase=phase*deg)
        readout()
    return exp

axis_phases = SweepParameter("rf_axis_phase", [0,90,180,270], "deg")
axis_preview = lab.compile(rf_phase_scan(q0,axis_phases,rf_pi2,**callbacks))
allxy_experiments = [rf_word_experiment(q0,word,rf_gate_map,name=f"allxy_{i:02}",**callbacks)
                     for i,word in enumerate(analysis.ALLXY_PAIRS)]
allxy_preview = lab.compile(allxy_experiments[6])
amplification_preview = lab.compile(rf_word_experiment(q0,["x"]*8,rf_gate_map,name="rf_area_amplification",**callbacks))
display([{"index":i,"pulses":" → ".join(pair),"ideal_P1":round(p,5)}
           for i,(pair,p) in enumerate(zip(analysis.ALLXY_PAIRS,analysis.allxy_expected()))])
""")
md(r"""
### 7.1 面积误差放大与二维图的模型读法

固定参考半 π 后重复成对的 Xπ，理想 P1=1/2，小面积误差随重复数放大。半 π 版本在参考半 π 后，每组打四个待校半 π；N=0 是参考点。只扫描待校 pulse 的幅度，不能把参考半 π 也一起改动。
这与正负轴抵消序列不同；同时看失谐、ALLXY 和 loss，不能把长序列的任一极值当成正确工作点。
下面两张图是**孤立两能级、无噪声 MODEL**，不是 Spectrum 输出推算的原子人口，更不是测量；Ramsey 模型包括有限 detuned 半 π pulse。真实图必须使用逐 shot 分类数据。
""")
code("""
area_blocks = SweepParameter("area_blocks", [0,1,3], "dimensionless")
rf_area_pi = Square(duration=q0.parameters.rf.pi_duration,amplitude=rf_amplitudes)
rf_area_half = Square(duration=q0.parameters.rf.pi2_duration,amplitude=rf_amplitudes)

def rf_area_amplification(q, amplitude, blocks, reference_half, test_pulse, *, pulses_per_block, prepare, readout, shots=2):
    exp = Experiment(f"rf_area_{pulses_per_block}_pulse_blocks",[q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(blocks), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            play(q.rf,reference_half)
            with exp.repeat(blocks):
                for _ in range(pulses_per_block):
                    play(q.rf,test_pulse)
        readout()
    return exp

area_pi_preview = lab.compile(rf_area_amplification(q0,rf_amplitudes,area_blocks,rf_pi2,rf_area_pi,pulses_per_block=2,**callbacks))
area_half_preview = lab.compile(rf_area_amplification(q0,rf_amplitudes,area_blocks,rf_pi2,rf_area_half,pulses_per_block=4,**callbacks))
model_detuning_hz = np.linspace(-4000,4000,101)
model_rabi_time_s = np.linspace(0,800e-6,121)
model_ramsey_time_s = np.linspace(0,2e-3,121)
model_rabi_hz = 1/(2*q0.parameters.rf.pi_duration)
model_chevron = analysis.rabi_probability(model_rabi_time_s[None,:],model_detuning_hz[:,None],model_rabi_hz)
model_ramsey = analysis.ramsey_probability(model_ramsey_time_s[None,:],model_detuning_hz[:,None],model_rabi_hz,q0.parameters.rf.pi2_duration)
fig,axes = plt.subplots(1,2,figsize=(10,3))
for ax,times,values,title in zip(axes,[model_rabi_time_s,model_ramsey_time_s],[model_chevron,model_ramsey],["MODEL Rabi chevron","MODEL finite-pulse Ramsey"]):
    im = ax.pcolormesh(times*1e6,model_detuning_hz/1e3,values,shading="auto",vmin=0,vmax=1)
    ax.set(xlabel="Pulse / dark time (us)",ylabel="Drive - transition (kHz)",title=title)
    fig.colorbar(im,ax=ax,label="P1")
fig.savefig(OUTPUT/"model_2d_rabi_ramsey.png",dpi=150,bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT/"model_2d_rabi_ramsey.png")))
""")
md(r"""
## 8. Global-RF single-qubit randomized benchmarking

抽样完整 24 个单比特 Clifford，冻结随机种子和精确 inverse；同一条随机序列广播给所有原子，各 site 独立拟合 $P_0(m)=A p^m+B$。在标准 Markovian、弱 gate dependence、无显著泄漏等适用条件下，单比特每 Clifford 平均误差 $r_C=(1-p)/2$。不除以一个任意脉冲数就宣称 primitive gate fidelity。

这不是各 site 独立抽样的 simultaneous RB。全局 RF 不支持把现有 `num_qubits=2` Clifford 目录直接映射到 q0/q1；其中局域门会被广播，逆门关系随之失效。

本节把全部过程写在 notebook：**2×2 门定义 → 固定 24 Clifford → 用户 basis → 最优分解 → 随机 Clifford 与 recovery → native word → length × sequence 实验扫描 → 拟合**。不调用库内 RB 生成器，也不使用上一节的 RF word 实验代替完整扫描。

正式采数可从 8–12 个深度、每深度数十条序列开始，根据 pilot decay 扩展。保留 seed/sequence/site/shot，按随机序列而不是按散落 shot bootstrap。下面冻结 48 条，默认编译 3 个长度 × 2 条序列；文件中明确记录预览范围，不表示其余电路已经编译或执行。

### 8.1 从熟悉的 2×2 酉矩阵到精确的 Bloch 旋转

$U_a(\theta)=\cos(\theta/2)I-i\sin(\theta/2)\sigma_a$ 作用于态矢量；$\rho=(I+\mathbf r\cdot\boldsymbol\sigma)/2$ 对应 Bloch 向量。由 $U\rho U^\dagger$ 得到
$R_{ij}=\mathrm{Tr}(\sigma_i U\sigma_j U^\dagger)/2$，作用为 $\mathbf r'=R\mathbf r$。
全局相位在转换中消失；Clifford 的 R 是元素为 −1、0、1 的正交矩阵。**先检查，再取整**，不把非 Clifford 旋转近似投到目录。

所有角度参数使用弧度。代码中的门列表按执行顺序书写：先 A 后 B 对应矩阵 B @ A。数学门表达校准目标，不证明真实脉冲的保真度。
""")
code(
    r'''
from collections import deque
from dataclasses import fields, is_dataclass
from fractions import Fraction
from hashlib import sha256
from heapq import heappop, heappush

from scipy.optimize import least_squares

from taiyidsl import FlatTopGaussian, rz
from taiyidsl.units import Quantity, as_fraction

RB_ATOL = 1e-10
RB_PAULI = {
    "x": np.array([[0, 1], [1, 0]], dtype=complex),
    "y": np.array([[0, -1j], [1j, 0]], dtype=complex),
    "z": np.array([[1, 0], [0, -1]], dtype=complex),
}


def rb_rotation(axis, angle):
    """绕指定 Pauli 轴的理想 2×2 酉矩阵；angle 单位是弧度。"""
    if axis not in RB_PAULI or not np.isfinite(angle):
        raise ValueError("Require axis x/y/z and a finite angle")
    return np.cos(angle / 2) * np.eye(2) - 1j * np.sin(angle / 2) * RB_PAULI[axis]


def rb_rf_unitary(angle, phase):
    """RF 脉冲目标：绕 cos(phase) X + sin(phase) Y 旋转 angle。"""
    if not np.isfinite([angle, phase]).all():
        raise ValueError("RF angle and phase must be finite")
    axis = np.cos(phase) * RB_PAULI["x"] + np.sin(phase) * RB_PAULI["y"]
    return np.cos(angle / 2) * np.eye(2) - 1j * np.sin(angle / 2) * axis


def chronological_unitary(unitaries):
    """依次执行输入矩阵；空序列是 I，后执行的矩阵乘在左边。"""
    total = np.eye(2, dtype=complex)
    for unitary in unitaries:
        total = unitary @ total
    return total


def same_unitary(left, right):
    """比较两个酉矩阵，允许任意整体相位；不忽略相对相位。"""
    overlap = np.trace(np.asarray(right).conj().T @ left) / 2
    if abs(overlap) < RB_ATOL:
        return False
    return np.allclose(left, overlap / abs(overlap) * right, atol=RB_ATOL, rtol=0)


def unitary_to_rotation(unitary):
    """验证 2×2 酉性和 Clifford 性质，再返回精确 3×3 整数旋转。

    每列是 U sigma_j U† 的 Pauli 系数。只有该作用为保持右手性的
    带符号坐标轴置换时才接受；例如 Rz(pi/4) 会报错，不做近似综合。
    """
    unitary = np.asarray(unitary, dtype=complex)
    if unitary.shape != (2, 2) or not np.isfinite(unitary).all():
        raise ValueError("Require a finite 2x2 unitary")
    if not np.allclose(unitary.conj().T @ unitary, np.eye(2), atol=RB_ATOL, rtol=0):
        raise ValueError("Matrix is not unitary")
    axes = tuple(RB_PAULI.values())
    rotation = np.array([
        [np.trace(a @ unitary @ b @ unitary.conj().T) / 2 for b in axes]
        for a in axes
    ])
    rounded = np.rint(rotation.real).astype(int)
    if (
        not np.allclose(rotation, rounded, atol=RB_ATOL, rtol=0)
        or not np.array_equal(rounded.T @ rounded, np.eye(3, dtype=int))
        or round(np.linalg.det(rounded)) != 1
    ):
        raise ValueError("Basis gates must be single-qubit Cliffords")
    return rounded


def rotation_key(rotation):
    """精确整数旋转按行展平，作为集合去重和 inverse 查表的键。"""
    return tuple(int(v) for v in rotation.flat)
''',
    tags=("rb-definitions",),
)
md(r"""
### 8.2 固定的 24 个 Clifford：身份与 native basis 分开

下面的固定 XY 生成元只用于给 24 个 Clifford 建立稳定编号。BFS 从 I 开始，记录每个首次出现的旋转及门词；6 个 X 轴去向 × 4 个垂直 Y 轴去向 = 24。生成元顺序与旧目录一致，所以 seed 对应的逻辑电路也保持一致。

这相当于自动填写参考 helper 的手写 Clifford 表，但其编号沿用本项目而非参考 helper。用户更换 RF basis 时**不改这个目录**，只重新计算下一节的分解。identity 的参考门词为空，不包含零幅度 pulse 或等待。
""")
code(
    r'''
RF_REFERENCE_UNITARIES = {
    "x90": rb_rotation("x", np.pi / 2),
    "xm90": rb_rotation("x", -np.pi / 2),
    "y90": rb_rotation("y", np.pi / 2),
    "ym90": rb_rotation("y", -np.pi / 2),
    "x180": rb_rotation("x", np.pi),
    "y180": rb_rotation("y", np.pi),
}


def build_clifford_catalog():
    """固定 XY 生成元的 BFS；返回稳定编号的旋转与 reference_word。

    queue 保证短门词先被访问；visited 按矩阵去重，不按字符串去重。
    这里的参考词用于教学和旧 optical RB，之后独立优化 RF native 词。
    """
    generators = {name: unitary_to_rotation(u) for name, u in RF_REFERENCE_UNITARIES.items()}
    identity = np.eye(3, dtype=int)
    queue = deque([(identity, ())])
    visited = {rotation_key(identity)}
    catalog = []
    while queue:
        rotation, word = queue.popleft()
        catalog.append({"rotation": rotation.copy(), "reference_word": word})
        for name, gate in generators.items():
            candidate = gate @ rotation
            key = rotation_key(candidate)
            if key not in visited:
                visited.add(key)
                queue.append((candidate, word + (name,)))
    if len(catalog) != 24:
        raise RuntimeError("The fixed reference generators must produce 24 Cliffords")
    return tuple(catalog)


def axis_images(rotation):
    """把三列转成例如 +X, +Z, -Y，便于直接阅读一个 Clifford。"""
    return tuple(
        ("+" if column[np.argmax(abs(column))] > 0 else "-")
        + "XYZ"[np.argmax(abs(column))]
        for column in rotation.T
    )
''',
    tags=("rb-definitions",),
)
code(r"""
def show_rb_table(rows):
    '''用 notebook 原生 Markdown 展示表格，长门词也能逐行比较。'''
    columns = tuple(rows[0])
    lines = [" | ".join(columns), " | ".join("---" for _ in columns)]
    for row in rows:
        lines.append(" | ".join(str(row[c]).replace("|", r"\|") for c in columns))
    display(Markdown("\n".join(lines)))


rb_catalog = build_clifford_catalog()
show_rb_table([
    {"Clifford": i, "X/Y/Z images": axis_images(c["rotation"]),
     "reference word": " → ".join(c["reference_word"]) or "I"}
    for i, c in enumerate(rb_catalog)
])
""")
md(r"""
### 8.3 用户 basis：理想门、物理实现与成本放在一起

每个门填写 unitary 和按时间排列的 implementation。RF 步骤引用已命名 pulse，并声明其已校准目标转角 angle 与驱动相位 phase；负 X 由正面积 pulse 加 π 相位实现。virtual_z 步骤只有 angle，最终直接调用 rz。

一个 basis 门可以包含多个步骤，理想乘积必须与 unitary 相同（允许全局相位）。可校验的是声明的一致性，实际 pulse 面积仍需实验标定。此示例接受固定、无相位调制的 Square 或 FlatTopGaussian；扫描 pulse 不适合作为已冻结的门标定。

成本是 **(RF pulse 数，RF pulse 总时长，总步骤数)**，由实现自动计算。时长用 Fraction 精确累加；virtual Z 前两项为零、步骤数为 1。正负 π 在 Clifford 数学上等价，但可有不同的校准实现；同成本时按 basis 的声明顺序决定选哪一个。identity 由空词表示，无需加入 basis。
""")
code(
    r'''
def rb_json_value(value):
    """保存静态 pulse 参数及数值；不把 Python 对象 repr 冒充实验记录。"""
    if isinstance(value, Quantity):
        return {"value": float(value), "dimension": value.dimension}
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, np.ndarray):
        return rb_json_value(value.tolist())
    if isinstance(value, np.generic):
        return rb_json_value(value.item())
    if is_dataclass(value):
        return {"type": type(value).__name__,
                **{f.name: rb_json_value(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {key: rb_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rb_json_value(item) for item in value]
    return value


def rb_pulse_duration(pulse):
    """从静态命名 pulse 读取精确秒数，不另外维护一份门时长。"""
    if isinstance(pulse, Square):
        duration = as_fraction(pulse.duration, "time")
    elif isinstance(pulse, FlatTopGaussian):
        duration = as_fraction(pulse.flat_duration, "time") + (
            2 * as_fraction(pulse.sigma, "time")
            * as_fraction(pulse.cutoff_sigma, "dimensionless")
        )
    else:
        raise ValueError("RB requires a static Square or FlatTopGaussian pulse")
    if duration <= 0 or pulse.phase_modulation is not None:
        raise ValueError("RB pulses need positive duration and no phase modulation")
    # Reject swept amplitude/frequency too: a frozen word needs fixed calibration.
    for value, dimension in ((pulse.amplitude, "dimensionless"), (pulse.frequency, "frequency")):
        if value is not None:
            as_fraction(value, dimension)
    return duration


def prepare_gate_basis(definitions, pulses):
    """检查用户 basis 的理想门和物理声明，并派生成本与整数旋转。

    pulses 是名字到命名 pulse 的映射。RF angle 是校准目标，不根据幅度
    猜测转角；phase 是 play 的显式覆盖值。只有整个 basis 门需是 Clifford，
    其多步实现的中间旋转可为非 Clifford，但整体必须精确对应声明的门。
    """
    if not definitions:
        raise ValueError("Gate basis must not be empty")
    basis = {}
    for name, definition in definitions.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Gate names must be nonempty strings")
        unitary = np.asarray(definition["unitary"], dtype=complex)
        rotation = unitary_to_rotation(unitary)
        implementation = tuple(dict(step) for step in definition["implementation"])
        if not implementation:
            raise ValueError(f"{name}: use the catalogue's empty word for identity")
        step_unitaries = []
        pulse_count, duration = 0, Fraction(0)
        for step in implementation:
            angle = float(step["angle"])
            if not np.isfinite(angle):
                raise ValueError(f"{name}: angle must be finite")
            if step["kind"] == "rf":
                if angle <= 0:
                    raise ValueError("Use positive RF area and phase to choose the axis")
                phase = float(step["phase"])
                step_unitaries.append(rb_rf_unitary(angle, phase))
                pulse_count += 1
                duration += rb_pulse_duration(pulses[step["pulse"]])
            elif step["kind"] == "virtual_z":
                step_unitaries.append(rb_rotation("z", angle))
            else:
                raise ValueError(f"Unknown implementation kind: {step['kind']}")
        if not same_unitary(chronological_unitary(step_unitaries), unitary):
            raise ValueError(f"{name}: implementation does not match the declared unitary")
        basis[name] = {
            "unitary": unitary.copy(), "rotation": rotation,
            "implementation": implementation,
            "cost": (pulse_count, duration, len(implementation)),
            "pulse_snapshots": {
                step["pulse"]: rb_json_value(pulses[step["pulse"]])
                for step in implementation if step["kind"] == "rf"
            },
        }
    return basis


def rb_basis_fingerprint(basis):
    """绑定数学门、实现、pulse 快照及声明顺序，防止旧电路被新 basis 执行。"""
    payload = [
        {"name": name, "rotation": gate["rotation"].tolist(),
         "implementation": gate["implementation"], "cost": gate["cost"],
         "pulse_snapshots": gate["pulse_snapshots"]}
        for name, gate in basis.items()
    ]
    encoded = json.dumps(rb_json_value(payload), sort_keys=True, allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def optimize_clifford_decompositions(catalog, basis):
    """在固定 24 节点图上寻找每个 Clifford 的最低字典序成本。

    边是在当前门词末尾追加一个 native gate，因此新旋转为 gate @ current。
    Dijkstra 每次展开当前最低成本；虚拟 Z 虽无 pulse/时长成本，仍有步骤
    成本，不会靠零成本循环产生无限长门词。严格改善才更新，稳定 serial 和
    basis 插入顺序共同决定同成本平局。这里不跨随机 Clifford 边界合并。
    """
    lookup = {rotation_key(c["rotation"]): i for i, c in enumerate(catalog)}
    costs, words = {0: (0, Fraction(0), 0)}, {0: ()}
    queue, serial = [(costs[0], 0, 0)], 0
    while queue:
        cost, _, index = heappop(queue)
        if costs[index] != cost:
            continue
        for name, gate in basis.items():
            target = lookup[rotation_key(gate["rotation"] @ catalog[index]["rotation"])]
            candidate = tuple(a + b for a, b in zip(cost, gate["cost"], strict=True))
            if target not in costs or candidate < costs[target]:
                costs[target] = candidate
                words[target] = words[index] + (name,)
                serial += 1
                heappush(queue, (candidate, serial, target))
    if len(costs) != 24:
        raise ValueError(f"Basis reaches only {len(costs)}/24 Cliffords")
    return tuple({"native_word": words[i], "cost": costs[i]} for i in range(24))
''',
    tags=("rb-definitions",),
)
code("""
# pulse 在第 2 节已命名；修改标定后重建 pulse，再重跑本节。
rb_pulses = {"rf_pi": rf_pi, "rf_pi2": rf_pi2}
rf_xy_basis = {
    "x90": {"unitary": rb_rotation("x", np.pi/2),
            "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": 0}]},
    "xm90": {"unitary": rb_rotation("x", -np.pi/2),
             "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": np.pi}]},
    "y90": {"unitary": rb_rotation("y", np.pi/2),
            "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": np.pi/2}]},
    "ym90": {"unitary": rb_rotation("y", -np.pi/2),
             "implementation": [{"kind": "rf", "pulse": "rf_pi2", "angle": np.pi/2, "phase": -np.pi/2}]},
    "x180": {"unitary": rb_rotation("x", np.pi),
             "implementation": [{"kind": "rf", "pulse": "rf_pi", "angle": np.pi, "phase": 0}]},
    "y180": {"unitary": rb_rotation("y", np.pi),
             "implementation": [{"kind": "rf", "pulse": "rf_pi", "angle": np.pi, "phase": np.pi/2}]},
}
rf_xvz_basis = {
    "x90": rf_xy_basis["x90"],
    "xm90": rf_xy_basis["xm90"],
    "x180": rf_xy_basis["x180"],
    "xm180": {"unitary": rb_rotation("x", -np.pi),
              "implementation": [{"kind": "rf", "pulse": "rf_pi", "angle": np.pi, "phase": np.pi}]},
    "z90": {"unitary": rb_rotation("z", np.pi/2),
            "implementation": [{"kind": "virtual_z", "angle": np.pi/2}]},
    "zm90": {"unitary": rb_rotation("z", -np.pi/2),
             "implementation": [{"kind": "virtual_z", "angle": -np.pi/2}]},
    "z180": {"unitary": rb_rotation("z", np.pi),
             "implementation": [{"kind": "virtual_z", "angle": np.pi}]},
}

# 唯一的 basis 选择入口；也可换成自己填写的同结构字典。
RB_BASIS_NAME = "X/VZ"
gate_basis = {"XY": rf_xy_basis, "X/VZ": rf_xvz_basis}[RB_BASIS_NAME]
rb_basis = prepare_gate_basis(gate_basis, rb_pulses)
rb_decompositions = optimize_clifford_decompositions(rb_catalog, rb_basis)
rb_xy_basis = prepare_gate_basis(rf_xy_basis, rb_pulses)
rb_xy_decompositions = optimize_clifford_decompositions(rb_catalog, rb_xy_basis)
rb_xvz_basis = prepare_gate_basis(rf_xvz_basis, rb_pulses)
rb_xvz_decompositions = optimize_clifford_decompositions(rb_catalog, rb_xvz_basis)

decomposition_comparison = []
for index in range(24):
    row = {"Clifford": index, "X/Y/Z images": axis_images(rb_catalog[index]["rotation"])}
    for label, table in (("XY", rb_xy_decompositions), ("X/VZ", rb_xvz_decompositions)):
        word, cost = table[index]["native_word"], table[index]["cost"]
        row.update({
            f"{label} word": " → ".join(word) or "I",
            f"{label} RF pulses": cost[0], f"{label} VZ": cost[2] - cost[0],
            f"{label} duration (us)": float(cost[1]) * 1e6,
        })
    decomposition_comparison.append(row)
show_rb_table(decomposition_comparison)
show_rb_table([
    {"basis": label, "mean RF pulses / Clifford": np.mean([d["cost"][0] for d in table]),
     "mean duration (us) / Clifford": np.mean([float(d["cost"][1]) * 1e6 for d in table])}
    for label, table in (("XY", rb_xy_decompositions), ("X/VZ", rb_xvz_decompositions))
])
""")
md(r"""
### 8.3.1 对着分解表理解：为什么平均只有 0.833 个 RF pulse？

以下统计对应上面的**默认 X/VZ basis 与当前优化表**，Clifford 编号从 0 开始。请先区分 native 操作数与 RF 物理脉冲数：virtual Z 是一项操作，但不产生 RF pulse，也没有脉冲时长成本。

| 表中的 Clifford 编号 | 分解类型 | 数量 | 每项 RF pulse 数 |
|---|---|---:|---:|
| 0、19、21、23 | 分别为 I、z180、zm90、z90；I 使用空门词 | 4 | 0 |
| 1–4、7–18 | 一个 RF π/2 脉冲，必要时配合 virtual Z | 16 | 1 |
| 5、6、20、22 | 一个 RF π 脉冲，必要时配合 virtual Z | 4 | 1 |

因此，从固定目录的 24 个 Clifford **均匀采样**时，平均 RF 数为

$$
\overline{N_{\rm RF}}=\frac{4\times0+16\times1+4\times1}{24}
=\frac{20}{24}=\frac56\approx0.8333.
$$

这不是“每个 Clifford 少于一个物理操作”，而是四个纯 Z／identity 元素不需要 RF，其余二十个都只需要一个 RF。作为对照，当前 XY 表中需要 0、1、2、3 个 RF 的 Clifford 分别有 1、6、13、4 个，平均为 $(6+26+12)/24=1.8333$。

当前命名 pulse 的 π/2 时长为 100 μs、π 时长为 200 μs，所以 X/VZ 的平均 RF 总时长是 $(16\times100+4\times200)/24=100$ μs。修改 basis 或 pulse 标定后，应以上方重新生成的表为准。

这个平均值统计**单个目录元素的分解成本**；有限条随机电路不会恰好达到这个平均值。RB 的 length 只计随机 Clifford，实际电路的 RF 总数还要加上 recovery 的分解。这里没有跨 Clifford 边界合并操作，也不能把 RB 拟合得到的 Clifford 误差直接除以 0.833 当作单脉冲误差。
""")
md(r"""
### 8.3.2 Clifford #15：Dijkstra 为什么选择一个 RF 加两个 virtual Z？

请在上方 24 项表中找到 **#15**：它将 X/Y/Z 轴分别映射为 $(+Z,-Y,+X)$，对应忽略全局相位后的 Hadamard 门。下面的箭头表示**从左到右的播放顺序**，矩阵乘积则是后执行的门写在左边。

| Basis | 优化门词 | RF 数 | RF 总时长 | 总操作数 |
|---|---|---:|---:|---:|
| XY | y90 → x180 | 2 | 300 μs | 2 |
| X/VZ | z90 → x90 → z90 | 1 | 100 μs | 3 |

用本节 $R_a(\theta)=e^{-i\theta\sigma_a/2}$ 的约定，两条门词满足

$$
R_x(\pi)R_y(\pi/2)
=R_z(\pi/2)R_x(\pi/2)R_z(\pi/2)
=-iH,\qquad H=\frac{X+Z}{\sqrt2}.
$$

搜索图的节点是固定的 24 个 Clifford；在门词末尾追加一个 basis gate，就是沿一条边移动到 `gate_rotation @ current_rotation` 对应的节点。Dijkstra 同时比较候选路径，优先取累计成本最小的节点继续展开。下表只追踪它**最终选中路径**上的累计成本，不是整个搜索的展开顺序：

| 已追加的门词 | 累计成本（RF 数，RF 时长，总操作数） |
|---|---|
| I：空门词 | (0, 0 μs, 0) |
| z90 | (0, 0 μs, 1) |
| z90 → x90 | (1, 100 μs, 2) |
| z90 → x90 → z90 | (1, 100 μs, 3) |

按“**RF 数 → 时长 → 操作数**”作字典序比较，$(1,100,3)$ 优于 XY 的 $(2,300,2)$，即使前者操作数更多。两套 basis 各自在自己的图中搜索；这里将它们的最优结果并排比较。virtual Z 的前两项成本为零，第三项为一，因此额外绕一圈的 Z 操作不会免费进入最优门词；完全同成本时按 basis 声明顺序稳定处理。

这个例子也有直观的 RF 数下界：纯 Z 旋转始终保持 Z 轴，而 #15 要将 Z 轴变成 X 轴，所以至少需要一个 RF。找到上述一脉冲实现后，RF 数已经最少；搜索再按时长和操作数决定具体门词。“最优”仅针对已声明的门与成本，不代表实测误差最低。

若 #15 单独从 frame $F=0$ 开始：第一个 z90 使 $F=-90^\circ$，x90 的实际 RF 轴相位成为 $-90^\circ$，最后一个 z90 将 $F$ 更新为 $-180^\circ$。因此完整的 H 实现包含**一次物理 RF 旋转和保留的相位参考**，不能只拿出那个 RF 脉冲就将其视为完整 H。8.5.2 节会展示 #15 放入真实随机电路后如何继承前一个 Clifford 的 frame。
""")
md(r"""
### 8.4 先抽逻辑 Clifford，再展开成所选 basis

length 统计随机 Clifford，排除最后一个 recovery；不是 RF pulse 数。每个 (seed, length, sequence) 独立初始化 RNG，从 0…23 均匀、有放回抽样。不同长度不是同一长序列的前缀；不同 sequence 也可能偶然相同。增加 shots 不重新抽门。

若随机序列整体为 $T=C_m\cdots C_1$，则恢复门为 $T^{-1}=T^T$。查固定目录得到 recovery_index，再用同一 basis 的优化表展开。**更换 basis 只改变 native word，抽中的 Clifford 和 recovery 编号保持不变。**
""")
code(
    r'''
def rb_integer_values(values, label):
    """扫描坐标必须为不重复的非负整数；拒绝 bool 和静默截断浮点数。"""
    values = tuple(values)
    if not values or any(
        isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) or v < 0
        for v in values
    ) or len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique nonnegative integers")
    return tuple(int(v) for v in values)


def sample_rb_circuits(lengths, *, sequence_count, seed, catalog):
    """冻结逻辑随机电路；不依赖 native basis、shot 或扫描排列顺序。"""
    lengths = rb_integer_values(lengths, "lengths")
    if type(sequence_count) is not int or sequence_count <= 0:
        raise ValueError("sequence_count must be a positive integer")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    lookup = {rotation_key(c["rotation"]): i for i, c in enumerate(catalog)}
    circuits = []
    for length in lengths:
        for sequence in range(sequence_count):
            rng = np.random.default_rng(np.random.SeedSequence([seed, length, sequence]))
            indices = [int(i) for i in rng.integers(0, len(catalog), size=length)]
            total = np.eye(3, dtype=int)
            for index in indices:
                total = catalog[index]["rotation"] @ total
            circuits.append({
                "seed": seed, "length": length, "sequence": sequence,
                "clifford_indices": indices,
                "recovery_index": lookup[rotation_key(total.T)],
            })
    return circuits


def decompose_rb_circuits(circuits, catalog, decompositions, basis):
    """按固定边界展开每个 Clifford，包括 recovery；不做跨边界消门。"""
    output = []
    basis_fingerprint = rb_basis_fingerprint(basis)
    for circuit in circuits:
        indices = [*circuit["clifford_indices"], circuit["recovery_index"]]
        words, total = [], np.eye(3, dtype=int)
        for index in indices:
            word = decompositions[index]["native_word"]
            rotation = np.eye(3, dtype=int)
            for name in word:
                rotation = basis[name]["rotation"] @ rotation
            if not np.array_equal(rotation, catalog[index]["rotation"]):
                raise ValueError(f"Decomposition does not implement Clifford {index}")
            total = rotation @ total
            words.append(list(word))
        if not np.array_equal(total, np.eye(3, dtype=int)):
            raise ValueError("RB recovery is not the ideal identity")
        native_word = [name for word in words for name in word]
        cost = tuple(sum((basis[name]["cost"][i] for name in native_word), Fraction(0) if i == 1 else 0)
                     for i in range(3))
        output.append({
            **circuit, "clifford_indices": list(circuit["clifford_indices"]),
            "basis_fingerprint": basis_fingerprint,
            "clifford_words": words, "native_word": native_word,
            "rf_pulse_count": cost[0], "virtual_z_count": cost[2] - cost[0],
            "pulse_duration_s": float(cost[1]),
        })
    return output


def rb_basis_snapshot(basis, pulses):
    """保存 basis 的数学身份、实现顺序、精确成本与实际使用的命名 pulse。"""
    used = {s["pulse"] for gate in basis.values() for s in gate["implementation"] if s["kind"] == "rf"}
    return {
        "fingerprint": rb_basis_fingerprint(basis),
        "cost_order": ["rf_pulse_count", "pulse_duration_s", "implementation_step_count"],
        "gates": {
            name: {"rotation": gate["rotation"].tolist(),
                   "unitary_real": gate["unitary"].real.tolist(),
                   "unitary_imag": gate["unitary"].imag.tolist(),
                   "implementation": rb_json_value(gate["implementation"]),
                   "cost": rb_json_value(gate["cost"])}
            for name, gate in basis.items()
        },
        "pulses": {name: rb_json_value(pulse) for name, pulse in pulses.items() if name in used},
    }
''',
    tags=("rb-definitions",),
)
code("""
RB_LENGTHS = (0, 1, 4, 8, 16, 32)
RB_SEQUENCE_COUNT = 8
RB_SEED = 171
rb_logical_circuits = sample_rb_circuits(
    RB_LENGTHS, sequence_count=RB_SEQUENCE_COUNT, seed=RB_SEED, catalog=rb_catalog
)
rb_sequences = decompose_rb_circuits(rb_logical_circuits, rb_catalog, rb_decompositions, rb_basis)
rb_short = next(s for s in rb_sequences if s["length"] == 4 and s["sequence"] == 0)
show_rb_table([
    {"position": i, "role": "recovery" if i == rb_short["length"] else "random",
     "Clifford": index, "native word": " → ".join(word) or "I"}
    for i, (index, word) in enumerate(zip(
        [*rb_short["clifford_indices"], rb_short["recovery_index"]],
        rb_short["clifford_words"], strict=True
    ))
])
print("完整 native 执行顺序:", " → ".join(rb_short["native_word"]))
print("RF pulses:", rb_short["rf_pulse_count"], "; virtual Z:", rb_short["virtual_z_count"])
""")
md(r"""
### 8.5 完整实验正文与小扫描预览

下面的函数只消费冻结电路，不在实验构建、shot 或编译时抽样。每个条件执行准备 → 10 μs 明示准备间隔 → 同一个 AWG block 内按顺序播放 native word → 读出。RF 使用 q0 的校准并广播到已登记原子；不能为每个 site 重复播放一遍。

rz(q, θ) 将 spin drive frame 减去 θ。其后 RF 的有效相位为 phase − 累积 θ；不能再手动补偿一次，或在门之间 reset。VZ 在逻辑上零时长，实际后端的更新约束仍由编译器检查。保留末尾的 VZ；Z-basis 人口读出不需要将它变成额外 RF pulse。

默认编译 lengths=(0,1,4)、sequence_ids=(0,1)，共 6 点，每点 2 shots。完整 48 点的 Experiment 也会建立；只有显式打开 COMPILE_FULL_RB 才编译它。更换 basis 或 pulse 标定后，要重新准备 basis、分解、展开序列和建立实验。
""")
code(
    r'''
def global_rf_rb(q, circuits, basis, pulses, *, lengths, sequence_ids, prepare, readout, shots=2):
    """完整 global RF RB 扫描，返回 Experiment；不连接硬件、不重抽随机门。

    circuits 含冻结的 native_word；basis 决定每个门具体的 RF/VZ 步骤。
    lengths × sequence_ids 必须全部存在。脉冲数量不等于 Clifford length；
    length=0 的空词只做准备/读出，不插入人为 identity 时槽。
    """
    lengths = rb_integer_values(lengths, "lengths")
    sequence_ids = rb_integer_values(sequence_ids, "sequence_ids")
    records = {(c["length"], c["sequence"]): c for c in circuits}
    if len(records) != len(circuits):
        raise ValueError("Duplicate frozen RB coordinates")
    if any((m, s) not in records for m in lengths for s in sequence_ids):
        raise ValueError("Missing frozen circuit for a requested length/sequence")
    selected = [records[m, s] for m in lengths for s in sequence_ids]
    # 即使成本相同，改了门或 pulse 参数也必须重新展开，不能执行旧 native_word。
    basis = prepare_gate_basis(basis, pulses)
    fingerprint = rb_basis_fingerprint(basis)
    if any(c.get("basis_fingerprint") != fingerprint for c in selected):
        raise ValueError("Gate basis or pulses changed: rebuild decompositions and circuits")
    exp = Experiment("global_rf_1q_rb", [q], metadata={
        "control": "same RF on every registered atom",
        "length_definition": "random Cliffords excluding recovery",
        "basis": rb_basis_snapshot(basis, pulses),
        "rb_sequences": selected, "frozen_circuit_count": len(circuits),
        "compiled_condition_selection": {"lengths": list(lengths), "sequence_ids": list(sequence_ids)},
        "hardware_executed": False,
    })
    q = exp.qubits[q.uid]
    exp.metadata["reference_qubit_snapshot"] = q.to_dict()
    length_axis = SweepParameter("rb_length", lengths, "dimensionless")
    sequence_axis = SweepParameter("rb_sequence", sequence_ids, "dimensionless")
    with exp.average(shots), exp.sweep(length_axis), exp.sweep(sequence_axis), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            for length in lengths:
                with exp.case(length_axis, length):
                    for sequence_id in sequence_ids:
                        with exp.case(sequence_axis, sequence_id):
                            for gate_name in records[length, sequence_id]["native_word"]:
                                for step in basis[gate_name]["implementation"]:
                                    if step["kind"] == "rf":
                                        play(q.rf, pulses[step["pulse"]], phase=step["phase"])
                                    else:  # validated virtual_z
                                        rz(q, step["angle"])
        readout()
    return exp
''',
    tags=("rb-definitions",),
)
code("""
RB_PREVIEW_LENGTHS = (0, 1, 4)
RB_PREVIEW_SEQUENCE_IDS = (0, 1)
COMPILE_FULL_RB = False

rb_experiment = global_rf_rb(
    q0, rb_sequences, rb_basis, rb_pulses,
    lengths=RB_LENGTHS, sequence_ids=range(RB_SEQUENCE_COUNT), **callbacks,
)
rb_preview_experiment = global_rf_rb(
    q0, rb_sequences, rb_basis, rb_pulses,
    lengths=RB_PREVIEW_LENGTHS, sequence_ids=RB_PREVIEW_SEQUENCE_IDS, **callbacks,
)
rb_preview = lab.compile(rb_preview_experiment)
rb_full_compiled = lab.compile(rb_experiment) if COMPILE_FULL_RB else None
rb_manifest = {
    "catalog": "fixed_xy_24_reference_order", "basis_name": RB_BASIS_NAME,
    "basis": rb_basis_snapshot(rb_basis, rb_pulses),
    "reference_qubit_snapshot": rb_experiment.metadata["reference_qubit_snapshot"],
    "clifford_catalog": [
        {"index": i, "rotation": c["rotation"].tolist(), "reference_word": list(c["reference_word"])}
        for i, c in enumerate(rb_catalog)
    ],
    "decompositions": rb_json_value(rb_decompositions),
    "circuits": rb_sequences,
    "preview": {"lengths": list(RB_PREVIEW_LENGTHS), "sequence_ids": list(RB_PREVIEW_SEQUENCE_IDS),
                "compiled_points": len(rb_preview.points), "shots": rb_preview.shots},
    "full_compiled": rb_full_compiled is not None,
    "hardware_executed": False,
}
(OUTPUT / "rf_rb_sequences.json").write_text(
    json.dumps(rb_manifest, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8"
)
display({"frozen_circuits": len(rb_sequences), "preview_points": len(rb_preview.points),
         "preview_shots": rb_preview.shots, "full_compiled": rb_manifest["full_compiled"]})

# 检查真正编译的条件与 RF 广播范围；不重复编译。
assert [p.axis_indices for p in rb_preview.points] == [(i, j) for i in range(3) for j in range(2)]
for point in rb_preview.points:
    record = next(c for c in rb_sequences
                  if (c["length"], c["sequence"]) == (point.coordinates["rb_length"], point.coordinates["rb_sequence"]))
    events = [e for e in point.logical_events if e.kind == "play" and e.signal == q0.rf]
    assert len(events) == record["rf_pulse_count"]
    assert all(e.params["global_operation"]["targets"] == ["q0", "q1", "spectator"] for e in events)
    assert sum(e.kind == "rz" for e in point.logical_events) == record["virtual_z_count"]
    assert sum(e.kind == "reset_phase" for e in point.logical_events) == 1
    if record["length"] == 0:
        assert not events
""")
md(r"""
### 8.5.1 两套 basis 的 pulse sheet 与最终 DAC waveform

下面对**同一批随机 Clifford、同一个 recovery、同样的 6 个条件与 2 shots**分别导出 XY 和 X/VZ 报告；已编译的所选 basis 直接复用。两套 pulse 数与时长不同是预期结果，不要求 DAC 波形相同。

| Basis | 总览 | Pulse sheet | Waveform simulator |
|---|---|---|---|
| XY | [报告](../../output/yb171_tuneup/rf_rb_basis_comparison/xy/index.html) | [时序](../../output/yb171_tuneup/rf_rb_basis_comparison/xy/pulse_sheet.html) | [最终 DAC](../../output/yb171_tuneup/rf_rb_basis_comparison/xy/waveform.html) |
| X/VZ | [报告](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/index.html) | [时序](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/pulse_sheet.html) | [最终 DAC](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/waveform.html) |

建议先检查 **point 4：length=4、sequence=0**，两套使用随机编号 [19,15,13,1] 与 recovery 19。查看器 Advanced 中可筛选 point/shot；pulse sheet 的 rz 是零时长标记，waveform 只显示实际输出，不会为 virtual Z 添加 pulse。零长度条件没有 RF 波形是正常的。

报告目录还保存逐门 phase_trace.csv/json，将每个 native 步骤与编译后的逻辑事件对应；phase 为 RF 轴相位，不包含随时间演化的载波项。相位记录保留末尾 virtual Z，即使它后面不再有 pulse。这是离线编译输出，不是量子态仿真或实测 RB 保真度。
""")
code(
    r"""
import csv

RB_REPORT_ROOT = OUTPUT / "rf_rb_basis_comparison"
rb_basis_previews, rb_basis_reports = {}, {}
rb_report_summary = {}
for label, slug, basis, table in (
    ("XY", "xy", rb_xy_basis, rb_xy_decompositions),
    ("X/VZ", "xvz", rb_xvz_basis, rb_xvz_decompositions),
):
    circuits = decompose_rb_circuits(rb_logical_circuits, rb_catalog, table, basis)
    assert all(
        (left["clifford_indices"], left["recovery_index"])
        == (right["clifford_indices"], right["recovery_index"])
        for left, right in zip(circuits, rb_sequences, strict=True)
    )
    if rb_basis_fingerprint(basis) == rb_basis_fingerprint(rb_basis):
        compiled = rb_preview
    else:
        comparison_experiment = global_rf_rb(
            q0, circuits, basis, rb_pulses,
            lengths=RB_PREVIEW_LENGTHS, sequence_ids=RB_PREVIEW_SEQUENCE_IDS,
            **callbacks,
        )
        compiled = lab.compile(comparison_experiment)
    rb_basis_previews[label] = compiled
    directory = RB_REPORT_ROOT / slug
    rb_basis_reports[label] = compiled.export_report(directory)
    compiled.assert_integrity()

    by_condition = {(c["length"], c["sequence"]): c for c in circuits}
    phase_trace, conditions = [], []
    for point in compiled.points:
        record = by_condition[point.coordinates["rb_length"], point.coordinates["rb_sequence"]]
        conditions.append({
            "point": point.index, "length": record["length"], "sequence": record["sequence"],
            "clifford_indices": record["clifford_indices"],
            "recovery_index": record["recovery_index"],
            "rf_pulses": record["rf_pulse_count"], "virtual_z": record["virtual_z_count"],
            "duration_us": record["pulse_duration_s"] * 1e6,
        })
        steps = [
            (position, index, name, step)
            for position, (index, word) in enumerate(zip(
                [*record["clifford_indices"], record["recovery_index"]],
                record["clifford_words"], strict=True,
            ))
            for name in word for step in basis[name]["implementation"]
        ]
        events = [
            event for event in point.logical_events
            if event.kind == "rz" or (event.kind == "play" and event.signal == q0.rf)
        ]
        origin = next(e.start_mu for e in point.logical_events if e.kind == "reset_phase")
        frame = 0.0
        for (position, index, name, step), event in zip(steps, events, strict=True):
            before, base, effective = frame, None, None
            if step["kind"] == "virtual_z":
                assert event.kind == "rz"
                np.testing.assert_allclose(event.params["angle"], step["angle"], atol=1e-12, rtol=0)
                frame -= step["angle"]
            else:
                assert event.kind == "play"
                base, effective = step["phase"], event.params["phase"]
                np.testing.assert_allclose(effective, base + frame, atol=1e-12, rtol=0)
            phase_trace.append({
                "point": point.index, "length": record["length"], "sequence": record["sequence"],
                "clifford_position": position, "clifford_index": index,
                "role": "recovery" if position == record["length"] else "random",
                "gate": name, "kind": step["kind"],
                "start_us_from_rb_entry": (event.start_mu - origin) * compiled.setup.tick_seconds * 1e6,
                "duration_us": event.duration_mu * compiled.setup.tick_seconds * 1e6,
                "angle_deg": float(np.rad2deg(step["angle"])),
                "frame_before_deg": float(np.rad2deg(before)),
                "frame_after_deg": float(np.rad2deg(frame)),
                "base_phase_deg": None if base is None else float(np.rad2deg(base)),
                "compiled_phase_deg": None if effective is None else float(np.rad2deg(effective)),
                "compiled_phase_mod360_deg": None if effective is None else float(np.rad2deg(effective) % 360),
            })
    (directory / "phase_trace.json").write_text(
        json.dumps(phase_trace, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    if phase_trace:
        with (directory / "phase_trace.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(phase_trace[0]))
            writer.writeheader()
            writer.writerows(phase_trace)
    rb_report_summary[label] = {
        "basis": rb_basis_snapshot(basis, rb_pulses),
        "conditions": conditions, "shots": compiled.shots,
        "mean_rf_pulses_per_clifford": sum(d["cost"][0] for d in table) / 24,
        "report": str(rb_basis_reports[label]["index"].relative_to(OUTPUT)),
        "hardware_executed": False,
    }
(RB_REPORT_ROOT / "comparison.json").write_text(
    json.dumps(rb_report_summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
)
show_rb_table([
    {"basis": label, **row}
    for label, summary in rb_report_summary.items()
    for row in summary["conditions"] if row["length"] == 4 and row["sequence"] == 0
])
""",
    tags=("rb-report-export",),
)
md(r"""
### 8.5.2 对着 point 4 波形检查 virtual Z 的累积相位

以下例子对应默认 `seed=171`、预览 `lengths=(0,1,4)`、`sequence_ids=(0,1)`，因此 **point 4 是 length=4、sequence=0**。修改扫描顺序后请按坐标寻找电路，不要仅依赖 point 编号。打开上方两套查看器，在 Advanced 中选 point 4、shot 0，并对照 [X/VZ 逐门相位 CSV](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/phase_trace.csv) 或 [JSON](../../output/yb171_tuneup/rf_rb_basis_comparison/xvz/phase_trace.json)。这些本地文件由前一个代码单元导出。

两套 basis 使用同一组随机 Clifford `[19,15,13,1]`，整体 recovery 是 #19。竖线只表示 Clifford 边界，不插入等待，也不重置 frame；最后一组是 recovery：

```text
Clifford：  #19          | #15          | #13       | #1  | #19 recovery
XY：        x180 y180    | y90 x180     | y90 x90   | x90 | x180 y180
X/VZ：      z180         | z90 x90 z90  | x90 z90   | x90 | z180
```

当前编译器对 $R_z(\theta)=e^{-i\theta Z/2}$ 使用如下相位记账：每条电路起点重置 $F=0$；遇到 virtual Z 时更新 frame，播放 RF 时将 frame 加到基础轴相位上：

$$
F\leftarrow F-\theta,\qquad
\phi_{\rm RF}=\phi_{\rm base}+F
=\phi_{\rm base}-\sum_{\text{此前 VZ}}\theta.
$$

所以正的 z90 会使后续 RF 轴相位减去 90°，zm90 则使它增加 90°。这里的 $\phi_{\rm RF}$ 是 RF **轴相位**；最终 DAC 波形还包含载波随时间的演化，不能把整个正弦波的瞬时相位都当成这个参数。

X/VZ 的三个 x90 基础相位都为 0°，完整八步如下。时间相对于 RB 控制起点，不包含 preparation/readout 的时长；同一时刻的零时长操作仍按门词顺序处理。

| 步骤 | Clifford / 角色 | native 操作 | 起点（μs） | F：操作前 → 后 | 实际 RF 轴相位（模 360°） |
|---|---|---|---:|---|---|
| 1 | #19 / random | z180 | 0 | 0° → −180° | 无 RF |
| 2 | #15 / random | z90 | 0 | −180° → −270° | 无 RF |
| 3 | #15 / random | x90，100 μs | 0 | −270° → −270° | −270° ≡ 90° |
| 4 | #15 / random | z90 | 100 | −270° → −360° | 无 RF |
| 5 | #13 / random | x90，100 μs | 100 | −360° → −360° | −360° ≡ 0° |
| 6 | #13 / random | z90 | 200 | −360° → −450° | 无 RF |
| 7 | #1 / random | x90，100 μs | 200 | −450° → −450° | −450° ≡ 270° |
| 8 | #19 / recovery | z180 | 300 | −450° → −630° | 无 RF |

注意这里 #15 的 RF 相位是 **90°**，而 8.3.2 中单独执行 #15 时是 **−90°**：区别来自前一个 #19 留下的 $F=-180^\circ$。Clifford 边界不会清除这个累积 frame。

在 pulse sheet 中应能对应到三个 RF 块和五个零时长 rz 标记；waveform 中只有三个 RF 脉冲，轴相位依次为 **90°、0°、270°**。同一条件的 XY 分解有 **9 个 RF、总计 1400 μs**，X/VZ 有 **3 个 RF、总计 300 μs**，两者都包含 recovery。这里比较的是 RB 脉冲部分，不是含准备、触发和读出的完整 shot 长度。

末尾 recovery 的 z180 虽然后面没有 RF，仍需保留：它使最终 $F=-630^\circ$。把所有 virtual Z 移到矩阵乘积左端，可以写成

$$
U_{\rm logical}=R_z(\Theta)U_{\rm RF},\qquad
\Theta=\sum\theta_{\rm VZ}=630^\circ,\quad F=-\Theta.
$$

完整逻辑电路（含 recovery）是忽略全局相位的 identity；单看物理 RF 脉冲的乘积则不必是 identity，还需要上述相位参考。末尾 Z 不改变本例 Z 基人口读出的概率，因此无需追加 RF 脉冲；若继续接相位敏感的操作，仍必须沿用该 frame。不要为了让 waveform 出现 recovery 的“形状”而补一个占位 pulse 或等待。
""")
md(r"""
### 8.6 拟合也直接展示：合成示例与实测数据分开

对每个 site 分别汇总每条随机序列的 P0，再等权平均到各 length，拟合 $A p^m+B$，报告 $r_C=(1-p)/2$。下面保留原示例的有界最小二乘：B 和 fraction 都在 [0,1]，A=(1−B)fraction，p 在 [0,1]。这样整条衰减位于概率范围内；不提供置信区间或自动校准写回。

这是 **SYNTHETIC** 拟合接线示例，数据不来自上面的编译或原子动力学。正式分析须检查拟合残差、SPAM、漂移、泄漏及适用的噪声假设，并按随机序列重采样估计不确定度；不能将 Clifford 误差直接除以一个平均脉冲数冒充 primitive fidelity。
""")
code(
    r'''
def fit_rb_decay(depths, probabilities):
    """拟合 A*p**m+B；保持原教学示例的约束，不将拟合解释为实测保真度。"""
    depths, probabilities = np.asarray(depths, dtype=float), np.asarray(probabilities, dtype=float)
    if (
        depths.ndim != 1 or probabilities.shape != depths.shape or len(depths) < 4
        or not np.isfinite(depths).all() or not np.isfinite(probabilities).all()
        or np.any(depths < 0) or len(np.unique(depths)) < 4
        or np.any((probabilities < 0) | (probabilities > 1))
    ):
        raise ValueError("Require finite probability data at >=4 distinct nonnegative depths")
    if np.ptp(probabilities) < 1e-10:
        raise ValueError("Constant data do not identify a decay rate")

    def residual(parameters):
        offset, fraction, p = parameters
        return offset + (1 - offset) * fraction * p**depths - probabilities

    candidates = [
        least_squares(residual, [min(probabilities) * .8, .8, p], bounds=(0, 1),
                      ftol=1e-12, xtol=1e-12, gtol=1e-12)
        for p in (.8, .95, .995)
    ]
    result = min(candidates, key=lambda item: np.sum(item.fun**2))
    offset, fraction, p = result.x
    return {"A": float((1 - offset) * fraction), "p": float(p), "B": float(offset),
            "residual_rms": float(np.sqrt(np.mean(result.fun**2))),
            "at_boundary": bool(np.any((result.x < 1e-6) | (result.x > 1 - 1e-6)))}
''',
    tags=("rb-definitions",),
)
code("""
# SYNTHETIC decay: independent of the compiled pulse program.
depths = np.array([0, 1, 4, 8, 16, 32, 64, 128])
rb_demo = .47 * .993**depths + .50
rb_fit = fit_rb_decay(depths, rb_demo)
print("Synthetic fit:", rb_fit, "; illustrative r_Clifford =", (1 - rb_fit["p"]) / 2)
""")
md(r"""
## 9. Clock spectroscopy、length Rabi 与状态选择性

统一 RF π 将 00 变为 11（spectator 也被翻转），再探测选定 $|1\rangle\leftrightarrow|c\rangle$。分别用初态 0 对照错误分支激发，扫描偏振/B/功率和 AOM 频率以确认选择性。区外原子的光移与散射由 spectator Ramsey 检查。

读出必须区分 P(c)、P(1)、loss；本版 native fixture 仅保留接口，不能仅把剩余亮度叫 clock transfer。spectrum 看漂移/边带/功率展宽，Rabi 看 Ω、return loss 与分支混入。下面 RF 初态与光探测在同一个 triggered sequence 内显式排序。
""")
code("""
clock_frequencies = optical_frequency_axis(q0,"clock",[-5000,0,5000])
clock_probe = Square(duration=100*us,amplitude=.08,frequency=clock_frequencies)
clock_lengths = SweepParameter("clock_length",[25,50,100,150],"us")
clock_rabi_pulse = Square(duration=clock_lengths,amplitude=q0.parameters.clock.pi_amplitude)

def clock_scan(q, axis, pulse, rf_inversion, *, name, prepare_one=True, prepare, readout, shots=2):
    exp = Experiment(name,[q],metadata={"readout_required":"logical / c / loss"})
    with exp.average(shots), exp.sweep(axis), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            if prepare_one:
                play(q.rf,rf_inversion)
            play(q.clock,pulse)
        readout()
    return exp

clock_spec_preview = lab.compile(clock_scan(q0,clock_frequencies,clock_probe,rf_pi,name="clock_spectroscopy",**callbacks))
clock_rabi_preview = lab.compile(clock_scan(q0,clock_lengths,clock_rabi_pulse,rf_pi,name="clock_length_rabi",**callbacks))
clock_wrong_branch = clock_scan(q0,clock_frequencies,clock_probe,rf_pi,name="clock_wrong_branch",prepare_one=False,**callbacks)
""")
md(r"""
### 9.1 Clock 二维 chevron 与 Ramsey

先 RF π 制备逻辑 1，再驱动共同 clock 分支。频率轴由光学失谐除以实测 AOM 倍率得到，负倍率和双程都不能丢掉。
Rabi 用 P(c)/P(1)/loss 找转移点；Ramsey 看光学相干、激光噪声和运动的组合，不能全归因于原子 T2。两原子仍共同受光。参考第 9 节的错误初态支路，确认态选择性。
""")
code("""
clock_map_frequency = optical_frequency_axis(q0,"clock",[-2000,0,2000])
clock_map_time = SweepParameter("clock_map_time", [50,100,150], "us")
clock_dark_time = SweepParameter("clock_dark_time", [10,100,500], "us")
clock_map_pulse = Square(duration=clock_map_time,amplitude=q0.parameters.clock.pi_amplitude)

def clock_chevron(q, frequency, duration, probe, rf_inversion, *, prepare, readout, shots=2):
    exp = Experiment("clock_chevron_2d",[q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            play(q.clock,probe,frequency=frequency)
        readout()
    return exp

def clock_ramsey_map(q, frequency, dark_time, rf_inversion, half_pulse, *, prepare, readout, shots=2):
    exp = Experiment("clock_ramsey_frequency_time",[q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(dark_time), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            play(q.clock,half_pulse,frequency=frequency)
            delay(q.clock,dark_time)
            play(q.clock,half_pulse,frequency=frequency)
        readout()
    return exp

clock_chevron_preview = lab.compile(clock_chevron(q0,clock_map_frequency,clock_map_time,clock_map_pulse,rf_pi,**callbacks))
clock_ramsey_map_preview = lab.compile(clock_ramsey_map(q0,clock_map_frequency,clock_dark_time,rf_pi,clock_pi2,**callbacks))
""")
md(r"""
## 10. 两种 clock phase calibration，不能混为一谈

**光学 Ramsey**：在 $|1\rangle,|c\rangle$ 两能级上做 clock π/2–τ–π/2，检查激光频率/相干与光学轴相位。电频率 detuning 需通过 AOM scale 换算。

**核自旋 shelving 往返相位**：RF π/2–[clock π 上行–hold–π 下行]–RF π/2，扫描末次 RF 相位，并与等时长无光对照比较。理想同相 clock π+π 会给 $|1\rangle$ 一个负号，人口返回不代表核自旋 identity。往返 phase 包括光学相位差、clock/Rydberg light shift、hold 中相位和搬运路径，不能由 DSL 的两个独立 frame 自动推断。

固定 RF/clock reference，扫描 clock 下行相位、hold 和上行前等待，分离确定相位与噪声；补偿斜率和符号以实测为准。对照需匹配整段时长与陷阱操作，不能拿零时长 idle 作基线。
""")
code("""
def optical_ramsey(q,tau,rf_inversion,pi2_pulse,*,optical_detuning_hz=1000.,prepare,readout,shots=2):
    frequency = (q.parameters.clock.frequency+optical_detuning_hz/optical_mapping["clock"]["frequency_scale"])*Hz
    exp = Experiment("clock_optical_ramsey",[q])
    with exp.average(shots),exp.sweep(tau),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            play(q.clock,pi2_pulse,frequency=frequency)
            delay(q.clock,tau)
            play(q.clock,pi2_pulse,frequency=frequency)
        readout()
    return exp

def clock_roundtrip_phase(q,phase,rf_half,clock_inversion,*,hold=10*us,cycles=1,light_on=True,
                          return_phase=0*rad,prepare,readout,shots=2):
    exp = Experiment("clock_roundtrip_phase" if light_on else "matched_idle_phase",[q],
                     metadata={"clock_cycles":cycles,"light_on":light_on})
    with exp.average(shots),exp.sweep(phase),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_half)
            for _ in range(cycles):
                if light_on:
                    play(q.clock,clock_inversion)
                    delay(q.clock,hold)
                    play(q.clock,clock_inversion,phase=return_phase)
                else:
                    delay(q.rf,2*q.parameters.clock.pi_duration+float(hold))
            play(q.rf,rf_half,phase=phase)
        readout()
    return exp

clock_tau = SweepParameter("clock_tau",[10,100,500],"us")
phase_axis = SweepParameter("analysis_phase",[0,90,180,270],"deg")
clock_optical_preview = lab.compile(optical_ramsey(q0,clock_tau,rf_pi,clock_pi2,**callbacks))
clock_phase_preview = lab.compile(clock_roundtrip_phase(q0,phase_axis,rf_pi2,clock_pi,**callbacks))
idle_phase_preview = lab.compile(clock_roundtrip_phase(q0,phase_axis,rf_pi2,clock_pi,light_on=False,**callbacks))

phi = np.linspace(0,2*np.pi,24,endpoint=False)
fringe_reference = .5+.45*np.cos(phi+.12)
fringe_clock = .5+.42*np.cos(phi+.12+.37)
fit_reference = analysis.fit_fringe(phi,fringe_reference)
fit_clock = analysis.fit_fringe(phi,fringe_clock)
phase_shift = np.angle(np.exp(1j*(fit_clock["phase_rad"]-fit_reference["phase_rad"])))
print("SYNTHETIC round-trip fringe shift (rad):",phase_shift)
""")
md(r"""
### 10.1 Shelving hold × RF analysis phase，与等时 light-off 参考

这一图检查 clock 往返给核自旋带来的相位与 contrast，不是上一节的光学 Ramsey。
light-off 分支保留两个 clock pulse 槽和相同 hold；不能把“有光等待更久”的相位差归因于光移。每个 hold 比较 fringe，相位差除以 hold 只有在线性区才可解释为频移。
""")
code("""
shelving_holds = SweepParameter("shelving_hold", [10,100,500], "us")
shelving_phases = SweepParameter("shelving_analysis_phase", [0,90,180,270], "deg")

def shelving_phase_map(q, hold, phase, rf_half, clock_inversion, *, light_on, return_phase=0*rad, prepare, readout, shots=2):
    exp = Experiment(f"shelving_phase_map_{light_on}",[q])
    with exp.average(shots), exp.sweep(hold), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_half)
            if light_on:
                play(q.clock,clock_inversion)
                delay(q.clock,hold)
                play(q.clock,clock_inversion,phase=return_phase)
            else:
                delay(q.rf,clock_inversion.duration)
                delay(q.rf,hold)
                delay(q.rf,clock_inversion.duration)
            play(q.rf,rf_half,phase=phase)
        readout()
    return exp

shelving_map_preview = lab.compile(shelving_phase_map(q0,shelving_holds,shelving_phases,rf_pi2,clock_pi,light_on=True,**callbacks))
shelving_idle_preview = lab.compile(shelving_phase_map(q0,shelving_holds,shelving_phases,rf_pi2,clock_pi,light_on=False,**callbacks))
""")
md(r"""
### 10.2 差分 light-shift Ramsey：probe amplitude × analysis phase

RF π/2 → 离共振 clock probe → RF analysis，扫描 probe 幅度和末 pulse 相位。幅度 0 的同长 pulse 是 matched reference；光学链路的真实消光仍要测量。
probe 的失谐只是示例，实验室需确认远离所有相关分支。测 phase、contrast、c 残留和 loss，不能在 DSL 人为加 phase shift 代替原子的光移。storage spectator 的读出也记录，但它是否真正受杂散光照射只能实测。
""")
code("""
stark_amplitudes = SweepParameter("clock_probe_amplitude", [0,.04,.08], "dimensionless")
stark_phases = SweepParameter("stark_analysis_phase", [0,90,180,270], "deg")
stark_frequency = (q0.parameters.clock.frequency + 50000/optical_mapping["clock"]["frequency_scale"])*Hz
stark_probe = Square(duration=100*us,amplitude=stark_amplitudes,frequency=stark_frequency)

def clock_light_shift(q, amplitude, phase, half_pulse, probe, *, prepare, readout, shots=2):
    exp = Experiment("clock_light_shift_ramsey",[q])
    with exp.average(shots), exp.sweep(amplitude), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,half_pulse)
            play(q.clock,probe)
            play(q.rf,half_pulse,phase=phase)
        readout()
    return exp

stark_preview = lab.compile(clock_light_shift(q0,stark_amplitudes,stark_phases,rf_pi2,stark_probe,**callbacks))
""")
md(r"""
## 11. Clock benchmarking：人口往返、相干往返、optical RB

三项回答不同问题：

1. 初态 1，重复完整 π–hold–π，测逻辑返回、clock 残留和存活；再用初态 0 做错误分支对照。重复数 n=0/1/2/4/8/…，每次都完整往返，不把相干人口振荡强行拟合单指数。
2. 用上一节 RF Ramsey 外壳增加 cycles，比较相位斜率与 contrast 衰减。它检验 shelving 对逻辑相干的影响。
3. 在光学两能级 $\{|1\rangle,|c\rangle\}$ 上跑 24-Clifford RB，若能任意设置 clock 脉冲相位并分辨两态。它测的是 optical qubit Clifford 性能，**不等于核自旋 CZ 中 shelving pair 的误差**。冷却/陷阱/光路应与真实 CZ 一致。
""")
code("""
clock_gate_map = {name:(clock_pi if name in ("x180","y180") else clock_pi2,phase)
                  for name,(_,phase) in rf_gate_map.items() if name in ("x90","xm90","y90","ym90","x180","y180")}

def clock_return_train(q,rf_inversion,clock_inversion,*,cycles,hold=10*us,prepare,readout,shots=2):
    exp = Experiment("clock_population_return",[q],metadata={"cycles":cycles})
    with exp.average(shots),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            for _ in range(cycles):
                play(q.clock,clock_inversion)
                delay(q.clock,hold)
                play(q.clock,clock_inversion)
        readout()
    return exp

def clock_rb(q,sequence,rf_inversion,gate_map,*,prepare,readout,shots=2):
    exp = Experiment("clock_optical_rb",[q],metadata={"rb":sequence,"encoding":"1,c optical subspace"})
    with exp.average(shots),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            for gate_name in sequence["native_word"]:
                pulse,phase = gate_map[gate_name]
                play(q.clock,pulse,phase=(phase/optical_mapping["clock"]["phase_scale"])*deg)
        readout()
    return exp

return_train_preview = lab.compile(clock_return_train(q0,rf_pi,clock_pi,cycles=2,**callbacks))
# 同一逻辑电路，独立展开固定参考 XY 词；RF 的 virtual Z 属于 spin frame，
# 不能当作 optical clock frame 的相位门。此处保留原来的 clock 脉冲顺序。
clock_rb_logical = next(c for c in rb_logical_circuits if c["length"] == 4 and c["sequence"] == 0)
clock_rb_short = {
    **clock_rb_logical,
    "basis": "fixed_xy_reference",
    "native_word": [
        name
        for index in [*clock_rb_logical["clifford_indices"], clock_rb_logical["recovery_index"]]
        for name in rb_catalog[index]["reference_word"]
    ],
}
clock_rb_preview = lab.compile(clock_rb(q0,clock_rb_short,rf_pi,clock_gate_map,**callbacks))
""")
md(r"""
### 11.1 Clock shelve–hold–return 与残留态对照

三个同长实验分别为 shelve/return、shelve/不 return、无 shelving。最后一个 pulse 槽用等时 delay 替代。
比较 c 态残留、返回的 1、ground 其它态和真实 loss；只有能够区分这些分支时才讨论 metastable lifetime，单条 return 曲线还混有转移误差和读出误差。
""")
code("""
def clock_population_hold(q, hold, rf_inversion, clock_inversion, *, shelve=True, return_to_spin=True, prepare, readout, shots=2):
    exp = Experiment(f"clock_hold_{shelve}_{return_to_spin}",[q])
    with exp.average(shots), exp.sweep(hold), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            play(q.rf,rf_inversion)
            if shelve:
                play(q.clock,clock_inversion)
            else:
                delay(q.clock,clock_inversion.duration)
            delay(q.clock,hold)
            if shelve and return_to_spin:
                play(q.clock,clock_inversion)
            else:
                delay(q.clock,clock_inversion.duration)
        readout()
    return exp

clock_hold_return_preview = lab.compile(clock_population_hold(q0,shelving_holds,rf_pi,clock_pi,**callbacks))
clock_hold_residual_preview = lab.compile(clock_population_hold(q0,shelving_holds,rf_pi,clock_pi,return_to_spin=False,**callbacks))
clock_hold_idle_preview = lab.compile(clock_population_hold(q0,shelving_holds,rf_pi,clock_pi,shelve=False,**callbacks))
""")
md(r"""
## 12. Rydberg spectroscopy、Rabi 与 blockade 对照

时序为准备 1 → clock π 到 c → UV probe → 分态/回收读出。先用**单原子装载的 zone**或足够远的原子校准裸跃迁/Ω，再用目标 pair 间距检查 collective 增强与双激发抑制。固定两原子 zone 同时准备 cc 的谱/Rabi 是受相互作用影响的 pair 数据，不能当裸单原子标定。

本节默认仍是 q0/q1 同照的 pair 预览。单原子对照必须实际单占据或真实搬走另一原子，并同步接线/位置与 occupancy 记录；仅去掉 q1 的 optical 路由或写 `targets=(q0,)` 都不能阻止光照到仍在 zone 内的原子。

扫频时检查 Zeeman/杂散态、light shift、离化/丢失；扫时长时检查 decay 和 recapture。blockade 对照同时报告 P(rr)、P(cr)+P(rc)、P(cc)、loss；若实验只能看原子损失，必须用额外回收/反激发/场离化标定才能把 loss 换成 Rydberg population。通过距离扫描、功率扫描和寿命 hold 检查 $V/\Omega$ 与短寿命的权衡。
""")
code("""
rydberg_frequencies = optical_frequency_axis(q0,"rydberg",[-100000,0,100000])
rydberg_probe = Square(duration=2*us,amplitude=.08,frequency=rydberg_frequencies)
rydberg_lengths = SweepParameter("rydberg_length",[.5,1.,2.,3.],"us")
rydberg_rabi_pulse = Square(duration=rydberg_lengths,amplitude=q0.parameters.rydberg.pi_amplitude)

def rydberg_scan(q,axis,probe,rf_inversion,clock_inversion,*,name,prepare,readout,shots=2):
    exp = Experiment(name,[q],metadata={"zone_occupancy":"two atoms; interacting pair", "readout_required":"c/r/ground/loss discrimination"})
    with exp.average(shots),exp.sweep(axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play(q.clock,clock_inversion)
            play(q.rydberg,probe)
        readout()
    return exp

rydberg_spec_preview = lab.compile(rydberg_scan(q0,rydberg_frequencies,rydberg_probe,rf_pi,clock_pi,name="pair_rydberg_spectrum",**callbacks))
rydberg_rabi_preview = lab.compile(rydberg_scan(q0,rydberg_lengths,rydberg_rabi_pulse,rf_pi,clock_pi,name="pair_rydberg_rabi",**callbacks))
""")
md(r"""
### 12.1 Pair Rydberg chevron 与返回保持诊断

二维 UV chevron 先保持在 c/r manifold 读出，沿用一维谱/Rabi 的结束态；不会偷偷加 clock 返回改变测量。
真正的单原子 A-only、B-only 与 pair 对照要替换真实装载/准备，并记录 occupancy；仅删掉 target 或 qubit 路由不能移走另一个受光原子。
共同 pair 的增强振荡和 Prr 抑制需一起分析，不能套单原子 Rabi 拟合。[Gaëtan et al.](https://arxiv.org/abs/0810.2960)给出 collective blockade 的实验依据。

另给 UV π–hold–π 返回诊断，之后 clock 返回 ground。命名 π 来自单原子标定，在 pair 内不保证 π 转移。该实验不是纯 Rydberg T1：衰变分支、再激发、trap off/on 和 recapture 都会改变观测；当前 fixture 不控制 trap，因此不生成假寿命拟合。
""")
code("""
rydberg_map_frequency = optical_frequency_axis(q0,"rydberg",[-100000,0,100000])
rydberg_map_time = SweepParameter("rydberg_map_time", [1,2,3], "us")
rydberg_map_pulse = Square(duration=rydberg_map_time,amplitude=q0.parameters.rydberg.pi_amplitude)
rydberg_holds = SweepParameter("rydberg_hold", [1,5,10], "us")

def rydberg_chevron(q, frequency, duration, probe, rf_inversion, clock_inversion, *, prepare, readout, shots=2):
    exp = Experiment("pair_rydberg_chevron_2d",[q],metadata={"occupancy":"interacting pair; requires joint c/r/ground/loss readout"})
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play(q.clock,clock_inversion)
            play(q.rydberg,probe,frequency=frequency)
        readout()
    return exp

def rydberg_return_hold(q, hold, rf_inversion, clock_inversion, uv_inversion, *, prepare, readout, shots=2):
    exp = Experiment("pair_rydberg_return_hold",[q],metadata={"interpretation":"return diagnostic, not isolated Rydberg T1"})
    with exp.average(shots), exp.sweep(hold), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play(q.clock,clock_inversion)
            play(q.rydberg,uv_inversion)
            delay(q.rydberg,hold)
            play(q.rydberg,uv_inversion)
            play(q.clock,clock_inversion)
        readout()
    return exp

rydberg_chevron_preview = lab.compile(rydberg_chevron(q0,rydberg_map_frequency,rydberg_map_time,rydberg_map_pulse,rf_pi,clock_pi,**callbacks))
rydberg_return_preview = lab.compile(rydberg_return_hold(q0,rydberg_holds,rf_pi,clock_pi,rydberg_pi,**callbacks))
""")
md(r"""
## 13. 定义完整 CZ：共同照明的两段 blockade pulse

不使用需要 control/target 分别寻址的 π–2π–π 序列。本版采用两个同长、同失谐、第二段改相位的 zone-wide UV pulse。clock 只 shelf 逻辑 1，因此单激发支路与 cc 集体支路获得不同相位。

数值辅助中的旋转约定为 $R_\phi(\theta)=\exp[-i\theta(\cos\phi X+\sin\phi Y)/2]$，UV 两能级 Hamiltonian 为
$H/\Omega=\begin{pmatrix}0&e^{-i\phi}/2\\e^{i\phi}/2&-\Delta/\Omega\end{pmatrix}$。
理想 blockade 下集体耦合为 $\sqrt2\Omega$。求解人口闭合和条件相位 π，得到用于**启动校准**的无量纲 seed。第二段相位符号依约定而变；文献的数字不能跳过 AOM phase mapping 直接照抄。

模型不含运动、衰减、光移、相位噪声和硬件传递函数；有限 blockade 可检查但仍不是完整物理仿真。π shelving/unshelving 的理想单原子负号已纳入 `logical_diag`。默认使用模型共同相位作为 placeholder，后续必须由原子数据标定。
""")
code("""
cz_seed = analysis.solve_collective_cz_seed()
cz_model = analysis.collective_cz_model(**cz_seed)
omega_rad_s = np.pi/q0.parameters.rydberg.pi_duration
duration_ideal_s = cz_seed["duration_omega"]/omega_rad_s
# Quantize demo duration to 8 ns; keep both ideal seed and compiled setting.
uv_duration_s = round(duration_ideal_s/8e-9)*8e-9
uv_detuning_hz = cz_seed["delta_over_omega"]*omega_rad_s/(2*np.pi)
uv_drive_frequency = (q0.parameters.rydberg.frequency + uv_detuning_hz/optical_mapping["rydberg"]["frequency_scale"])*Hz
uv_phase = (cz_seed["phase_rad"]/optical_mapping["rydberg"]["phase_scale"])*rad
clock_return_phase = (cz_model["clock_return_phase_rad"]/optical_mapping["clock"]["phase_scale"])*rad
uv_gate_pulse = Square(duration=uv_duration_s,amplitude=q0.parameters.rydberg.pi_amplitude)
CZ_GAP = 2*us
GATE_TIME_S = 2*q0.parameters.clock.pi_duration + 2*float(CZ_GAP) + 2*uv_duration_s

print("Ideal blockade seed:",cz_seed)
print("Ideal return:",cz_model["single_return"],cz_model["pair_return"])
print("Ideal / quantized UV duration (us):",duration_ideal_s*1e6,uv_duration_s*1e6)
print("Model-only outgoing clock phase (rad):",float(clock_return_phase))
quantized_model = analysis.collective_cz_model(cz_seed["delta_over_omega"],uv_duration_s*omega_rad_s,cz_seed["phase_rad"])
print("Quantized seed model return:",quantized_model["single_return"],quantized_model["pair_return"])

def play_full_gate(q,clock_inversion,uv_pulse,*,frequency,second_phase,return_phase,variant="cz"):
    # Ordinary Python authoring helper: appends plain DSL events, never hardware I/O.
    # Every 'cz' occurrence below contains a complete clock up/down cycle.
    if variant == "idle":
        delay(q.rf,GATE_TIME_S)
    elif variant == "clock":
        play(q.clock,clock_inversion)
        delay(q.clock,2*float(CZ_GAP)+2*uv_duration_s)
        # Same-phase ideal roundtrip is Z on each atom, not logical identity.
        play(q.clock,clock_inversion)
    elif variant == "cz":
        play(q.clock,clock_inversion)
        delay(q.clock,CZ_GAP)
        play(q.rydberg,uv_pulse,frequency=frequency)
        play(q.rydberg,uv_pulse,frequency=frequency,phase=second_phase)
        delay(q.clock,CZ_GAP)
        play(q.clock,clock_inversion,phase=return_phase)
    else:
        raise ValueError("variant must be idle, clock or cz")

gate_controls = {"frequency":uv_drive_frequency,"second_phase":uv_phase,"return_phase":clock_return_phase}
""")
md(r"""
### 13.1 先调闭合，再调相位；保留有限 blockade 误差

建议依次扫 Ω、Δ、duration、第二段 phase，先用单原子/原子对的 return/leakage 约束缩小候选区，再进行回波相位优化。下面先给显式二维 closure，再给相位×频率回波图；其它维度在 host 固定为候选，不把全部组合一次送进仪器。

以下图只展示同一理想 seed 在有限 $V/\Omega$ 下的模型偏离，不是实测误差预算。trap off/on、UV rise/fall 与 pulse 间的实际 gap 都要纳入实机重标定。不同 pair 的间距/Ω 不均匀时，共用 seed 不保证共用最优点。
""")
code("""
blockade_ratios = np.array([5,8,12,20,40,80,160],dtype=float)
finite_models = [analysis.collective_cz_model(**cz_seed,blockade_over_omega=b) for b in blockade_ratios]
fig, axes = plt.subplots(1,2,figsize=(8,3))
axes[0].semilogx(blockade_ratios,[1-m["pair_return"] for m in finite_models],"o-")
axes[0].set(xlabel="V / Omega",ylabel="1 - pair return",title="MODEL leakage")
axes[1].semilogx(blockade_ratios,[np.angle(np.exp(1j*(m["conditional_phase_rad"]-np.pi))) for m in finite_models],"o-")
axes[1].set(xlabel="V / Omega",ylabel="conditional phase - pi (rad)",title="MODEL phase error")
fig.savefig(OUTPUT / "model_finite_blockade.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "model_finite_blockade.png")))
""")
md(r"""
### 13.2 两段 CZ 的 duration × frequency closure 图

准备 11，完整 clock 上行 → 两段 UV → clock 下行，测 P11、其它逻辑人口、残留与 loss。只恢复人口不能证明纠缠：identity 也可以完全返回，因此 closure 只用于淘汰候选，下一步必须检查 χ 和共同相位。
这里每一段 UV 的时长相同，第二段 phase 固定；修改时长不复用旧的 GATE_TIME_S 对照等待。
""")
code("""
cz_closure_frequency = SweepParameter("cz_closure_hz", [float(uv_drive_frequency)+d for d in (-10000,0,10000)], "Hz")
cz_closure_duration = SweepParameter("cz_uv_duration", [uv_duration_s+d for d in (-80e-9,0,80e-9)], "s")
cz_closure_pulse = Square(duration=cz_closure_duration,amplitude=q0.parameters.rydberg.pi_amplitude)

def cz_closure_map(q, frequency, duration, rf_inversion, clock_inversion, uv_pulse, *, second_phase, return_phase, prepare, readout, shots=2):
    exp = Experiment("cz_two_pulse_closure_2d",[q])
    with exp.average(shots), exp.sweep(frequency), exp.sweep(duration), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_inversion)
            play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
        readout()
    return exp

cz_closure_preview = lab.compile(cz_closure_map(q0,cz_closure_frequency,cz_closure_duration,rf_pi,clock_pi,cz_closure_pulse,second_phase=uv_phase,return_phase=clock_return_phase,**callbacks))
""")
md(r"""
## 14. 全局回波校准纠缠相位 χ

采用分享分析的近似对角门 $G\doteq\mathrm{diag}(1,e^{i\delta_B},e^{i\delta_A},e^{i(\delta_A+\delta_B+\chi)})$。
按时间顺序做：**Yπ/2 → Gᴹ → Xπ → Gᴹ → Xπ → −Yπ/2 → readout**，所有 RF 均共同作用。
理想模型中局域静态相位被消去，$P_{11}=\sin^2(M\chi/2)$，$P_{01}=P_{10}=0$。先用 M=1 找工作区，再以 3、5 等奇数放大误差；极值无法给出误差正负，须双边扫描，避免锁到错误周期。

每条序列包含 **2M 次完整基态门**。回波成功不证明单个 CZ 的共同/差分局域相位正确；两次门之间的噪声、非对角误差、泄漏也不保证被抵消。优化 P11 时同时约束 logical survival、clock 残留与 loss。
""")
code("""
def cz_echo_phase(q,uv_phase_axis,rf_half,rf_inversion,clock_inversion,uv_pulse,*,
                  frequency,return_phase,M=1,prepare,readout,shots=2):
    exp = Experiment("cz_global_echo_phase",[q],metadata={"complete_cz_count":2*M,"M":M})
    with exp.average(shots),exp.sweep(uv_phase_axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half,phase=90*deg)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,
                               second_phase=uv_phase_axis,return_phase=return_phase)
            play(q.rf,rf_inversion)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,
                               second_phase=uv_phase_axis,return_phase=return_phase)
            play(q.rf,rf_inversion)
            play(q.rf,rf_half,phase=270*deg)
        readout()
    return exp

uv_phase_axis = SweepParameter("uv_second_phase",[float(uv_phase)+d for d in (-.08,0,.08)],"rad")
cz_echo_preview = lab.compile(cz_echo_phase(q0,uv_phase_axis,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,
                        frequency=uv_drive_frequency,return_phase=clock_return_phase,**callbacks))
cz_echo_preview.export_report(OUTPUT / "cz_echo_report")

def collective_rotation(theta,phase):
    r = analysis.rotation(theta,phase)
    return np.kron(r,r)

ket00 = np.array([1,0,0,0],complex)
Xg = collective_rotation(np.pi,0)
for chi in (.3,np.pi,3.4):
    D = np.diag(np.exp(1j*np.array([0,.31,-.24,.31-.24+chi])))
    E = Xg@D@Xg@D
    state = collective_rotation(-np.pi/2,np.pi/2)@E@collective_rotation(np.pi/2,np.pi/2)@ket00
    np.testing.assert_allclose(abs(state)**2,[np.cos(chi/2)**2,0,0,np.sin(chi/2)**2],atol=1e-12)
print("Independent ideal matrix check: echo cancels static local phases.")
""")
md(r"""
### 14.1 UV second phase × frequency：二维回波与误差放大

沿 closure 合格区域同时扫描 UV 相位和频率，用 M=1 定工作区，再用 M=3 放大。两个分支各有 M 次完整 gate；不会只 shelf 一次后重复 UV。
保留 P01/P10 与 loss，不能只优化 postselected P11。下面仅编译 M=1 的小网格；实际重复测量和 M=3 应由已通过 closure 的点开始。
""")
code("""
def cz_echo_map(q, frequency, phase, rf_half, rf_inversion, clock_inversion, uv_pulse, *, return_phase, M=1, prepare, readout, shots=2):
    exp = Experiment("cz_echo_phase_frequency",[q],metadata={"complete_cz_count":2*M})
    with exp.average(shots), exp.sweep(frequency), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half,phase=90*deg)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=phase,return_phase=return_phase)
            play(q.rf,rf_inversion)
            for _ in range(M):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=phase,return_phase=return_phase)
            play(q.rf,rf_inversion)
            play(q.rf,rf_half,phase=270*deg)
        readout()
    return exp

cz_echo_map_preview = lab.compile(cz_echo_map(q0,cz_closure_frequency,uv_phase_axis,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,return_phase=clock_return_phase,**callbacks))
""")
md(r"""
## 15. Bell 人口、共同相位与宇称

先假设 zone 两原子的局域相位近似相同。对已补偿的 $G_c=\mathrm{CZ}$，按分享分析的约定：
**Xπ/2 → Gc → −Yπ/2** 从 00 生成 $(|00\rangle+i|11\rangle)/\sqrt2$，目标 Bell 相位 $\beta_0=\pi/2$。

第一轮扫描共同补偿使奇数人口最小。这里用 **clock 返回 pulse 的相位**作为实际光学共同相位旋钮；也可用经校准的 RF frame tracking，但它只改变后续 RF 轴且同样影响 storage 原子，不能虚构局域 Z。两种方式不要重复补偿。

第二轮固定补偿，增加共同分析 π/2，扫描相位 ϑ。$\Pi=P_{00}+P_{11}-P_{01}-P_{10}=\Pi_0-C\cos(2\vartheta-\beta)$。
指定目标的 $F_{Bell,\beta_0}=[P_{00}+P_{11}+C\cos(\beta-\beta_0)]/2$。只取振幅会漏掉确定 Bell 相位错误；只测偶数人口有 π 歧义。它是该制备电路的输出态指标，不是平均 CZ fidelity。

共同补偿不能消除任意 $\delta_A-\delta_B$。若人口/相位无法同时对齐，应检查 zone 光强/光移、RF 不均匀、差分相位和泄漏，不借“单独搬运”假设局域 RF 条件 Ramsey 可用。
""")
code("""
def bell_population(q,return_phase_axis,rf_half,clock_inversion,uv_pulse,*,frequency,second_phase,prepare,readout,shots=2):
    exp = Experiment("bell_common_phase",[q])
    with exp.average(shots),exp.sweep(return_phase_axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half)
            play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase_axis)
            play(q.rf,rf_half,phase=270*deg)
        readout()
    return exp

def bell_parity(q,phase_axis,rf_half,clock_inversion,uv_pulse,*,frequency,second_phase,return_phase,prepare,readout,shots=2):
    exp = Experiment("bell_parity",[q])
    with exp.average(shots),exp.sweep(phase_axis),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half)
            play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
            play(q.rf,rf_half,phase=270*deg)
            play(q.rf,rf_half,phase=phase_axis)
        readout()
    return exp

return_phase_axis = SweepParameter("clock_return_phase",[float(clock_return_phase)+d for d in (-.2,0,.2)],"rad")
population_preview = lab.compile(bell_population(q0,return_phase_axis,rf_pi2,clock_pi,uv_gate_pulse,
                            frequency=uv_drive_frequency,second_phase=uv_phase,**callbacks))
parity_axis = SweepParameter("parity_phase",[0,45,90,135],"deg")
parity_preview = lab.compile(bell_parity(q0,parity_axis,rf_pi2,clock_pi,uv_gate_pulse,**gate_controls,**callbacks))

CZ = np.diag([1,1,1,-1])
bell = collective_rotation(np.pi/2,-np.pi/2)@CZ@collective_rotation(np.pi/2,0)@ket00
target_bell = np.array([1,0,0,1j])/np.sqrt(2)
assert np.isclose(abs(np.vdot(target_bell,bell))**2,1)
theta = np.linspace(0,2*np.pi,24,endpoint=False)
ideal_parity = np.array([np.dot([1,-1,-1,1],abs(collective_rotation(np.pi/2,t)@bell)**2) for t in theta])
np.testing.assert_allclose(ideal_parity,-np.cos(2*theta-np.pi/2),atol=1e-12)

# SYNTHETIC parity; fit both quadratures, not contrast alone.
parity_demo = .02-.86*np.cos(2*theta-(np.pi/2+.12))
design = np.column_stack([np.ones_like(theta),np.cos(2*theta),np.sin(2*theta)])
offset,b,c = np.linalg.lstsq(design,parity_demo,rcond=None)[0]
contrast = np.hypot(b,c)
beta = np.arctan2(-c,-b)
even_population_demo = .94
F_target_demo = (even_population_demo+contrast*np.cos(beta-np.pi/2))/2
print("SYNTHETIC target-phase Bell F:",F_target_demo,"; fitted beta:",beta)
fig,ax = plt.subplots(figsize=(7,3))
ax.plot(theta*180/np.pi,parity_demo,"o",label="Synthetic parity")
ax.plot(theta*180/np.pi,design@[offset,b,c],label="2-phase fit")
ax.set(xlabel="Global analysis phase (deg)",ylabel="Parity",title="SYNTHETIC Bell parity")
ax.legend()
fig.savefig(OUTPUT / "synthetic_bell_parity.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "synthetic_bell_parity.png")))
""")
md(r"""
### 可选扩展：共同输入角的 diagonal 模型辨识

有 site-resolved 四结果时，不必独立准备 01/10 也能对**特定对角模型**拟合相位：共同 $R_y(\theta)$ 制备，施加 G，再分别共同分析 X/Y，取 $\theta=\pi/3,2\pi/3$ 并用 π/2 交叉验证。令 $z_i=\langle X_i\rangle+i\langle Y_i\rangle$，则
$z_i/\sin\theta=A_i+B_i\sin^2(\theta/2)$，其中 $A_i=e^{i\delta_i}$、$B_i=A_i(e^{i\chi}-1)$。
由复线性拟合估计每 site 相位与共同 χ，并检查两个站点 χ 是否一致。这是本工作本的模型推导扩展，不是文献 full tomography。依赖已标定 RF/SPAM 与对角假设；仅测 parity、汇总 P01+P10，或仅在理想 χ=π 用 |++〉输入，可能丢失必要辨识信息。第一版主线仍用上面的回波与 Bell 实验。
""")
md(r"""
### 15.1 重复完整 CZ：相干误差与输入态相关损失

第一组是 **共同 Yπ/2 → Gⁿ → 共同分析 π/2**，扫 n 和分析相位；n=0 给参考，偶/奇 n 区分理想 CZ 周期与相位积累。它对共同相位和 coherent error 敏感，不能把振荡硬拟合成每门 fidelity。
第二组分别共同准备 00、++、11，再重复完整 G，**不加分析旋转**，测 logical survival、c/r 残留、loss 随 n 的变化。
这借鉴 [Ma et al. 2023，state-biased erasure](https://arxiv.org/abs/2305.05493) 的输入态比较；其 qubit 在 metastable manifold，本文是 ground-spin，且没有门中快速检测，故只是输入态相关损失诊断。没有中途探测就不能把最终 loss 定位到某个门。
""")
code("""
cz_repeat_counts = SweepParameter("complete_cz_repeats", [0,1,2], "dimensionless")
cz_repeat_phases = SweepParameter("cz_repeat_analysis_phase", [0,90,180,270], "deg")

def cz_repetition_map(q, count, phase, rf_half, clock_inversion, uv_pulse, *, frequency, second_phase, return_phase, prepare, readout, shots=2):
    exp = Experiment("cz_coherent_repetition",[q])
    with exp.average(shots), exp.sweep(count), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf,rf_half,phase=90*deg)
            with exp.repeat(count):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
            play(q.rf,rf_half,phase=phase)
        readout()
    return exp

def cz_input_loss_scan(q, count, rf_half, rf_inversion, clock_inversion, uv_pulse, *, input_state, frequency, second_phase, return_phase, prepare, readout, shots=2):
    if input_state not in ("00","++","11"):
        raise ValueError("input_state must be 00, ++ or 11")
    exp = Experiment(f"cz_input_loss_{input_state}",[q],metadata={"input_state":input_state,"readout":"joint logical/c/r/loss, no final RF analysis"})
    with exp.average(shots), exp.sweep(count), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            # Equal-length preparation slot for all three input states.
            if input_state == "11":
                play(q.rf,rf_inversion)
            elif input_state == "++":
                play(q.rf,rf_half,phase=90*deg)
                delay(q.rf,float(rf_inversion.duration)-float(rf_half.duration))
            else:
                delay(q.rf,rf_inversion.duration)
            with exp.repeat(count):
                play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=return_phase)
        readout()
    return exp

cz_repetition_preview = lab.compile(cz_repetition_map(q0,cz_repeat_counts,cz_repeat_phases,rf_pi2,clock_pi,uv_gate_pulse,**gate_controls,**callbacks))
cz_loss_zero_preview = lab.compile(cz_input_loss_scan(q0,cz_repeat_counts,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,input_state="00",**gate_controls,**callbacks))
cz_loss_plus_preview = lab.compile(cz_input_loss_scan(q0,cz_repeat_counts,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,input_state="++",**gate_controls,**callbacks))
cz_loss_one_preview = lab.compile(cz_input_loss_scan(q0,cz_repeat_counts,rf_pi2,rf_pi,clock_pi,uv_gate_pulse,input_state="11",**gate_controls,**callbacks))
""")
md(r"""
## 16. 全局回波随机序列：分离 RF / clock / 完整门

每 block 为 **Ri → G → Xπg → G**。理想 CZ 满足 CZ·Xg·CZ ≃ Yg，因此可计算只含共同 RF 的 recovery。clock/idle 的理想块不同，必须分别生成恢复，不能共用 CZ 版本的 recovery。

本版用随机共同半 π（Clifford 子集）实现 **GERB-style 诊断**，并非逐项复现论文的 Haar U-GERB。三版本分别是匹配等待、同相 clock 往返（理想为每原子 Z）、完整 CZ。每 block 含两次 G；对照等待匹配完整门时长。共享 seed 的随机 RF 相同，recovery 各算各的。

拟合 $A p^{N_{block}}+B$ 只是 block decay。不能令 F_CZ=p、盲取平方根或套 full-2Q 的 3/4 公式。回波会掩盖可重复的单比特相位偏差；因此下一节无回波 SSB 才是主要 CZ benchmark。
""")
code("""
def global_gate_word(q,word,gate_map,clock_inversion,uv_pulse,*,name,variant="cz",
                     frequency,second_phase,return_phase,prepare,readout,shots=2):
    exp = Experiment(name,[q],metadata={"word":list(word),"variant":variant,
                                       "complete_gate_count":sum(g in ("G","cz") for g in word)})
    with exp.average(shots),exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            for gate in word:
                if gate in ("G","cz"):
                    play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,
                                   return_phase=return_phase,variant=variant)
                else:
                    pulse,phase = gate_map[gate]
                    play(q.rf,pulse,phase=phase*deg)
        readout()
    return exp

echo_benchmark_previews = []
echo_words = {variant:analysis.global_echo_word(2,seed=171,variant=variant) for variant in ("idle","clock","cz")}
for variant,word in echo_words.items():
    unitary = analysis.global_echo_unitary(word,variant=variant)
    assert np.isclose(abs(unitary[0,0])**2,1)
    print(variant,word)
    echo_benchmark_preview = lab.compile(global_gate_word(q0,word,rf_gate_map,clock_pi,uv_gate_pulse,
                                     name=f"global_echo_random_{variant}",variant=variant,**gate_controls,**callbacks))
    echo_benchmark_previews.append(echo_benchmark_preview)
(OUTPUT/"echo_random_words.json").write_text(json.dumps(echo_words,indent=2),encoding="utf-8")
""")
md(r"""
## 17. 无回波 SSB：固定 RF 数量的主 benchmark

按分享分析与 [Tsai et al., PRX Quantum 6, 010331 (2025)](https://doi.org/10.1103/PRXQuantum.6.010331) 的对称稳定子思路，使用四种共同半 π {X90, −X90, Y90, −Y90} 与完整 CZ。由 00 出发可达 12 个对称稳定子态；辅助代码显式枚举，不依赖“随机走几步大概均匀”。

| 部分 | 固定模板 | 数量 |
|---|---|---|
| 初始化 | R1 → R2 → CZ → R3 → R4 → R5 | 5 RF + 1 CZ |
| 中段 | L 个随机 RF，前 n−2 个 RF 后各插一个 CZ | L RF + n−2 CZ |
| 恢复 | R1 → R2 → CZ → R3 → R4 | 4 RF + 1 CZ |

**每条 RF 半 π 总数=L+9，完整 CZ 总数=n（包含初始化/恢复）**。init 穷举 4⁵，recovery 穷举 4⁴，覆盖 12 态并逐条验证返回 00。每个深度均衡覆盖全部 12 个初态，每态独立随机词；理想门对这 12 态集合封闭，从均匀初态出发各层保持均匀分布。无固定 echo pair 消除门相位。

模板与固定 RF 开销对应论文 Fig. 2 / Appendix E；原论文采用 ⁸⁸Sr optical qubit。本版改为 ¹⁷¹Yb 的 RF 旋转、完整 clock 往返 CZ，并选择返回 00（论文为 11），查表保留等价门词。它不等于均匀抽样完整 2Q Clifford。训练种子用来优化共同补偿，固定参数后用新 seed 验证。不同 n 交错执行，并保留 pair/seed/init_state/sequence/shot。
""")
code("""
ssb_records = analysis.ssb_sequences(total_cz_counts=(2,4,6,10,14,18,26),random_rf_count=24,repeats_per_state=2,seed=171)
assert all(r["rf_count"]==33 and r["word"].count("cz")==r["total_cz_count"] for r in ssb_records)
for record in ssb_records:
    U = analysis.global_word_unitary(record["word"])
    assert np.isclose(abs(U[0,0])**2,1)
for n in (2,4,6,10,14,18,26):
    ids = [r["initial_state"] for r in ssb_records if r["total_cz_count"]==n]
    assert len(set(ids))==12 and all(ids.count(i)==2 for i in set(ids))
(OUTPUT/"ssb_sequences.json").write_text(json.dumps(ssb_records,indent=2),encoding="utf-8")
print("Frozen SSB sequences:",len(ssb_records),"; all ideal returns verified; only one compiled below.")
ssb_record = ssb_records[0]
ssb_preview = lab.compile(global_gate_word(q0,ssb_record["word"],rf_gate_map,clock_pi,uv_gate_pulse,
                      name="ssb_representative",**gate_controls,**callbacks))
""")
md(r"""
### 17.1 SSB 的拟合与可报告含义

短序列、小误差区间可用 $P_{return}(n)=A F_{SSB}^{n}$。这里的 F 是**协议推断的对称子空间指标**，不是把 depolarizing p 换算成 $(1+3p)/4$。它需要误差模型/深度稳定性检查；不能任意外推至很深序列，不能任意固定背景 1/4 或 1/3。

同时分析无条件 return、条件于 logical survival 的 return、atom survival、leakage。门依赖误差、慢漂移、泄漏返回和 coherent oscillation 可能使单指数失效。改变拟合深度、检查残差、使用独立 seed，并按**随机序列/初始化态分层重采样**给区间；同序列几百个 shot 不是几百条独立随机电路。

全局对称初态/控制主要探测 $\mathrm{span}\{|00\rangle,(|01\rangle+|10\rangle)/\sqrt2,|11\rangle\}$。对反对称 singlet 及其跨子空间相干的作用没有同样完整的覆盖。未来取得独立单比特控制后，才另做 full 2Q Clifford reference/interleaved RB。
""")
code("""
ssb_depths = np.array([2,4,6,10,14,18,26])
ssb_demo = .96*.992**ssb_depths
ssb_fit = analysis.fit_ssb_short(ssb_depths,ssb_demo)
print("SYNTHETIC short-depth protocol fit:",ssb_fit)
fig,axes = plt.subplots(1,2,figsize=(8,3))
pred = ssb_fit["A"]*ssb_fit["F_ssb"]**ssb_depths
axes[0].plot(ssb_depths,ssb_demo,"o",label="Synthetic return")
axes[0].plot(ssb_depths,pred,label="A F_ssb^n")
axes[0].set(xlabel="Complete CZ count (incl. init/recovery)",ylabel="P00",title="SYNTHETIC SSB")
axes[0].legend()
axes[1].plot(ssb_depths,ssb_demo-pred,"o-")
axes[1].set(xlabel="Complete CZ count",ylabel="Residual",title="Fit diagnostic")
fig.savefig(OUTPUT / "synthetic_ssb.png", dpi=150, bbox_inches="tight")
plt.close(fig)
display(Image(filename=str(OUTPUT / "synthetic_ssb.png")))
""")
md(r"""
### 17.2 固定深度 SSB 扫共同相位，再用独立 seed 验证

[Tsai / Endres 的 SSB 校准](https://arxiv.org/html/2407.20184v2)用固定深度序列扫描共同 Z 补偿，以无回波 return 找相位工作点。
这里把补偿放在每次完整 gate 的 outgoing clock pulse，相位符号由本工作本的 clock 映射决定。对同一候选保持随机词与初始化态完全相同，以减少比较噪声。
下面冻结全部 12 个初始化态的训练词，离线只编译第一条的三点相位扫描；实际候选评分要聚合所有态/序列，不能用第一条的偶然最大值。训练完成后固定 pulse 和补偿，再用独立 seed、多深度做 validation；不能把同一训练数据同时当验收。
""")
code("""
ssb_phase_records = analysis.ssb_sequences(total_cz_counts=(4,),random_rf_count=4,repeats_per_state=1,seed=1171)
ssb_validation_records = analysis.ssb_sequences(total_cz_counts=(2,4,6),random_rf_count=4,repeats_per_state=1,seed=2171)
ssb_return_phases = SweepParameter("ssb_clock_return_phase", [float(clock_return_phase)+d for d in (-.1,0,.1)], "rad")

def cz_ssb_phase_scan(q, phase, record, gate_map, clock_inversion, uv_pulse, *, frequency, second_phase, prepare, readout, shots=2):
    exp = Experiment("cz_ssb_common_phase_scan",[q],metadata={"ssb_record":record,"complete_gate_count":record["total_cz_count"]})
    with exp.average(shots), exp.sweep(phase), exp.realtime():
        prepare()
        delay(10*us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            for gate in record["word"]:
                if gate == "cz":
                    play_full_gate(q,clock_inversion,uv_pulse,frequency=frequency,second_phase=second_phase,return_phase=phase)
                else:
                    pulse,rf_phase = gate_map[gate]
                    play(q.rf,pulse,phase=rf_phase*deg)
        readout()
    return exp

ssb_phase_preview = lab.compile(cz_ssb_phase_scan(q0,ssb_return_phases,ssb_phase_records[0],rf_gate_map,clock_pi,uv_gate_pulse,frequency=uv_drive_frequency,second_phase=uv_phase,**callbacks))
for record in [*ssb_phase_records,*ssb_validation_records]:
    assert record["rf_count"] == 13
    assert np.isclose(abs(analysis.global_word_unitary(record["word"])[0,0])**2,1)
(OUTPUT/"ssb_phase_training.json").write_text(json.dumps(ssb_phase_records,indent=2),encoding="utf-8")
(OUTPUT/"ssb_phase_validation.json").write_text(json.dumps(ssb_validation_records,indent=2),encoding="utf-8")
""")
md(r"""
### 17.3 连续幅度／相位波形：候选定义与独立验收

前面的双方波门保留为基线。本节增加另一种**尚未校准的候选家族**：clock π 上行 → 2 μs → 一次连续 UV 波形 → 2 μs → clock π 下行。每次调用都是完整基态门；不会把 UV 切成小脉冲，也不在中途 reset frame。它不是某篇论文的 time-optimal 波形复现，也不保证这组演示参数能形成 CZ。文献方法与实现边界见[基准与波形文献对照](yb171_benchmarking_references.md)。

当前 DSL 支持 `FlatTopGaussian` 的归一化幅度包络，以及 `PolynomialPhase` 的局部相位；没有 `@waveform` 或任意 Python 函数波形接口。取 $u=t/T$，本例相位为 $\phi(u)=16\beta u^2(1-u)^2$：中心值 β，起终相位与斜率均为零。系数按升幂排列、单位 rad，`local` 模式足够。固定线性系数为零，避免把它与载频失谐作为两个重复自由度。

Gaussian 总时长为 `flat_duration + 2*cutoff_sigma*sigma`，包络**峰值**归一为 1，不按面积归一。Spectrum 使用 1.25 GS/s，必须显式设置 `sample_interval=0.8*ns`。改变总时长也改变相位导数对应的瞬时频率；看完整波形及频率诊断，不能只看名义载频。真实 Ω、AC Stark、传递函数与散射仍需原子／光学标定。

这里在 host 定义 β×平台时长的四个命名候选，先只选一个作软件预览。保持系数比例需要 host 重建 pulse，不能对 `SweepParameter` 做 `16*beta_axis`。旧 `collective_cz_model` 只描述两段方波，**不用于拟合本节波形**；本节也不生成虚构的优化结果。
""")

code("""
from taiyidsl import PolynomialPhase

shaped_candidates = []
uv_shaped_candidates = {}
for beta_optical in (-0.4, 0.4):
    for flat_us in (3.2, 4.0):
        candidate_id = f"smooth_beta{beta_optical:+.1f}_flat{flat_us:.1f}us"
        beta_drive = beta_optical / optical_mapping["rydberg"]["phase_scale"]
        phase_coefficients = [0, 0, 16 * beta_drive, -32 * beta_drive, 16 * beta_drive]
        candidate = {
            "candidate_id": candidate_id,
            "status": "uncalibrated; no atom data",
            "family": "flat_top_gaussian_and_zero_endpoint_polynomial_phase",
            "beta_optical_rad": beta_optical,
            "phase_coefficients_drive_rad": phase_coefficients,
            "phase_modulation_mode": "local",
            "flat_duration_us": flat_us,
            "sigma_us": 0.25,
            "cutoff_sigma": 4,
            "uv_total_duration_s": (flat_us + 2.0) * 1e-6,
            "sample_interval_s": 0.8e-9,
            "amplitude": q0.parameters.rydberg.pi_amplitude,
            "optical_detuning_hz": 0.0,
            "drive_frequency_hz": q0.parameters.rydberg.frequency,
            "clock_return_phase_rad": 0.0,
            "clock_gap_s": 2e-6,
            "pair": [q0.uid, q1.uid],
            "rf_spectator": spectator.uid,
            "required_initial_occupancy": "both pair sites loaded",
            "optical_mapping": dict(optical_mapping["rydberg"]),
        }
        shaped_candidates.append(candidate)
        uv_shaped_candidates[candidate_id] = FlatTopGaussian(
            flat_duration=flat_us * us,
            sigma=0.25 * us,
            cutoff_sigma=4,
            amplitude=candidate["amplitude"],
            sample_interval=0.8 * ns,
            phase_modulation=PolynomialPhase(tuple(phase_coefficients)),
        )

shaped_demo_candidate = shaped_candidates[0]  # Not an optimized selection.
uv_shaped_demo = uv_shaped_candidates[shaped_demo_candidate["candidate_id"]]
shaped_controls = {
    "frequency": shaped_demo_candidate["drive_frequency_hz"] * Hz,
    "amplitude": shaped_demo_candidate["amplitude"],
    "return_phase": shaped_demo_candidate["clock_return_phase_rad"] * rad,
}
display(shaped_candidates)
""")

md(r"""
### 17.4 同一候选的 closure、echo 与 Bell 检查

下面唯一的小操作函数 `play_shaped_gate` 直接列出一次完整门的五步时序。每个实验仍把准备、全局 RF、完整门顺序和读出写在正文中。每个实验在 sequence 入口将三个 frame 各 reset 一次，保持与前面 RF X/Y 轴相同的参考；完整门内部及重复门之间不 reset。clock/UV 仍共同照到 q0/q1，RF 还作用于 spectator。

closure 先准备 11，检查返回 11、clock/Rydberg 残留和 loss；单原子 closure 需要实际单占据准备，不能改 UID 冒充。echo 含 **2M 次完整门**，只在接近对角门时按前节解释 χ；Bell population/parity 各含一次完整门，必须同时检查共同相位与泄漏。以下四次编译只预览 closure 应力图、M=1 echo、四个 parity 相位和一条固定 SSB 序列；完整候选库不自动编译或运行。
""")

code("""
def play_shaped_gate(
    q, clock_inversion, uv_pulse, *, frequency, amplitude, return_phase
):
    play(q.clock, clock_inversion)
    delay(2 * us)
    play(q.rydberg, uv_pulse, frequency=frequency, amplitude=amplitude)
    delay(2 * us)
    play(q.clock, clock_inversion, phase=return_phase)


def shaped_pair_closure(
    q,
    amplitude_axis,
    frequency_axis,
    rf_inversion,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    return_phase,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_pair_closure",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "11 pair return",
            "complete_gate_count": 1,
            "readout_required": "joint logical/leak/loss",
        },
    )
    with exp.average(shots), exp.sweep(amplitude_axis), exp.sweep(frequency_axis):
        with exp.realtime():
            prepare()
            delay(10 * us)
            with exp.triggered_sequence(group="qubit_awg"):
                reset_phase(q.phase_frame("spin"))
                reset_phase(q.phase_frame("clock_control"))
                reset_phase(q.phase_frame("rydberg_control"))
                play(q.rf, rf_inversion)
                play_shaped_gate(
                    q,
                    clock_inversion,
                    uv_pulse,
                    frequency=frequency_axis,
                    amplitude=amplitude_axis,
                    return_phase=return_phase,
                )
            readout()
    return exp


def shaped_echo(
    q,
    rf_half,
    rf_inversion,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    return_phase,
    M=1,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_global_echo",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "global echo",
            "complete_gate_count": 2 * M,
            "M": M,
        },
    )
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf, rf_half, phase=90 * deg)
            for _ in range(M):
                play_shaped_gate(
                    q,
                    clock_inversion,
                    uv_pulse,
                    frequency=frequency,
                    amplitude=amplitude,
                    return_phase=return_phase,
                )
            play(q.rf, rf_inversion)
            for _ in range(M):
                play_shaped_gate(
                    q,
                    clock_inversion,
                    uv_pulse,
                    frequency=frequency,
                    amplitude=amplitude,
                    return_phase=return_phase,
                )
            play(q.rf, rf_inversion)
            play(q.rf, rf_half, phase=270 * deg)
        readout()
    return exp


def shaped_bell_population(
    q,
    return_phase_axis,
    rf_half,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_bell_population",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "Bell common-phase scan",
            "complete_gate_count": 1,
        },
    )
    with exp.average(shots), exp.sweep(return_phase_axis), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf, rf_half)
            play_shaped_gate(
                q,
                clock_inversion,
                uv_pulse,
                frequency=frequency,
                amplitude=amplitude,
                return_phase=return_phase_axis,
            )
            play(q.rf, rf_half, phase=270 * deg)
        readout()
    return exp


def shaped_bell_parity(
    q,
    phase_axis,
    rf_half,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    return_phase,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_bell_parity",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "Bell analysis phase",
            "complete_gate_count": 1,
            "target_bell_phase_rad": np.pi / 2,
        },
    )
    with exp.average(shots), exp.sweep(phase_axis), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            play(q.rf, rf_half)
            play_shaped_gate(
                q,
                clock_inversion,
                uv_pulse,
                frequency=frequency,
                amplitude=amplitude,
                return_phase=return_phase,
            )
            play(q.rf, rf_half, phase=270 * deg)
            play(q.rf, rf_half, phase=phase_axis)
        readout()
    return exp


shaped_stress_amplitude = SweepParameter(
    "shaped_stress_amplitude",
    [0.98 * shaped_controls["amplitude"], 1.02 * shaped_controls["amplitude"]],
    "dimensionless",
)
shaped_stress_detuning_hz = [-5000, 5000]
shaped_stress_frequency = SweepParameter(
    "shaped_stress_drive_hz",
    [
        shaped_demo_candidate["drive_frequency_hz"]
        + d / optical_mapping["rydberg"]["frequency_scale"]
        for d in shaped_stress_detuning_hz
    ],
    "Hz",
)
shaped_closure_preview = lab.compile(
    shaped_pair_closure(
        q0,
        shaped_stress_amplitude,
        shaped_stress_frequency,
        rf_pi,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        return_phase=shaped_controls["return_phase"],
        **callbacks,
    )
)
shaped_echo_preview = lab.compile(
    shaped_echo(
        q0,
        rf_pi2,
        rf_pi,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        M=1,
        **shaped_controls,
        **callbacks,
    )
)
shaped_parity_phase = SweepParameter("shaped_parity_phase", [0, 45, 90, 135], "deg")
shaped_parity_preview = lab.compile(
    shaped_bell_parity(
        q0,
        shaped_parity_phase,
        rf_pi2,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        **shaped_controls,
        **callbacks,
    )
)
# Build the population scan for inspection; do not compile an additional bank.
shaped_return_phase_axis = SweepParameter(
    "shaped_clock_return_phase", [-0.2, 0, 0.2], "rad"
)
shaped_population_exp = shaped_bell_population(
    q0,
    shaped_return_phase_axis,
    rf_pi2,
    clock_pi,
    uv_shaped_demo,
    candidate=shaped_demo_candidate,
    frequency=shaped_controls["frequency"],
    amplitude=shaped_controls["amplitude"],
    **callbacks,
)
""")

md(r"""
### 17.5 固定序列训练与独立 seed 验证

对候选比较，复用已经冻结的 SSB 字串：pair、12 个对称输入态的平衡、depth、每态重复数和 **33 个共同 RF 脉冲**全部固定。仅将每个 `cz` 标记替换为一次完整 shaped 候选门；init/recovery 也包括在总门数内。理想 CZ 的 recovery 是比较目标，不宣称未知候选具有该理想矩阵。

训练集合沿用 seed 171，held-out 使用新 seed 3171、相同 depth/态数/RF 数。选参只看训练集；参数与分析规则冻结后才看 held-out，不能看完再反复调参仍称独立验证。两份 manifest 保存完整字串，候选 manifest 保存 pair、读出要求、stress 设置和代码参数。重新装载、SPAM 漂移与 shot 随机化计划仍须由真实执行记录补齐。
""")

code("""
def shaped_ssb_word(
    q,
    record,
    gate_map,
    clock_inversion,
    uv_pulse,
    *,
    candidate,
    frequency,
    amplitude,
    return_phase,
    prepare,
    readout,
    shots=2,
):
    exp = Experiment(
        "shaped_ssb_fixed_word",
        [q],
        metadata={
            "candidate": candidate,
            "protocol": "fixed-word symmetric-state return",
            "ssb": dict(record),
            "complete_gate_count": record["total_cz_count"],
            "expected_readout": "00 only for the ideal target and calibrated SPAM",
        },
    )
    with exp.average(shots), exp.realtime():
        prepare()
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            reset_phase(q.phase_frame("spin"))
            reset_phase(q.phase_frame("clock_control"))
            reset_phase(q.phase_frame("rydberg_control"))
            for gate in record["word"]:
                if gate == "cz":
                    play_shaped_gate(
                        q,
                        clock_inversion,
                        uv_pulse,
                        frequency=frequency,
                        amplitude=amplitude,
                        return_phase=return_phase,
                    )
                else:
                    pulse, phase = gate_map[gate]
                    play(q.rf, pulse, phase=phase * deg)
        readout()
    return exp


shaped_ssb_train = [dict(record) for record in ssb_records]
shaped_ssb_depths = sorted({record["total_cz_count"] for record in shaped_ssb_train})
shaped_ssb_heldout = analysis.ssb_sequences(
    total_cz_counts=tuple(shaped_ssb_depths),
    random_rf_count=24,
    repeats_per_state=2,
    seed=3171,
)
for records in (shaped_ssb_train, shaped_ssb_heldout):
    assert all(record["rf_count"] == 33 for record in records)
    assert all(
        record["word"].count("cz") == record["total_cz_count"] for record in records
    )
    for depth in shaped_ssb_depths:
        states = [
            record["initial_state"]
            for record in records
            if record["total_cz_count"] == depth
        ]
        assert len(states) == 24 and all(
            states.count(state) == 2 for state in range(12)
        )
assert {record["seed"] for record in shaped_ssb_train} == {171}
assert {record["seed"] for record in shaped_ssb_heldout} == {3171}
assert {record["word"] for record in shaped_ssb_train}.isdisjoint(
    {record["word"] for record in shaped_ssb_heldout}
)

shaped_ssb_preview = lab.compile(
    shaped_ssb_word(
        q0,
        ssb_record,
        rf_gate_map,
        clock_pi,
        uv_shaped_demo,
        candidate=shaped_demo_candidate,
        **shaped_controls,
        **callbacks,
    )
)
shaped_candidate_manifest = {
    "status": "offline candidate definitions; no measured score",
    "candidates": shaped_candidates,
    "preview_candidate_id": shaped_demo_candidate["candidate_id"],
    "ssb_train_file": "shaped_ssb_train.json",
    "ssb_heldout_file": "shaped_ssb_heldout.json",
    "ssb_train_seed": 171,
    "ssb_heldout_seed": 3171,
    "ssb_total_cz_counts": shaped_ssb_depths,
    "ssb_rf_count": 33,
    "ssb_states": 12,
    "ssb_repeats_per_state": 2,
    "stress_amplitude_fraction": [0.98, 1.02],
    "stress_optical_detuning_hz": shaped_stress_detuning_hz,
    "readout_categories": ["00", "01", "10", "11", "leak", "loss", "invalid"],
    "hardware_validation": "not performed",
    "acceptance_policy": {
        "status": "freeze actual shot counts, confidence and thresholds before data",
        "constraints": [
            "invalid fraction",
            "loss / valid",
            "leak / valid",
            "logical / valid",
        ],
        "training_target": (
            "worst-group SSB return lower confidence bound; "
            "target Bell phase cross-check"
        ),
        "validation": (
            "held-out seed and all stress points; "
            "never an average gate fidelity claim"
        ),
    },
}
for name, payload in (
    ("shaped_candidate_manifest.json", shaped_candidate_manifest),
    ("shaped_ssb_train.json", shaped_ssb_train),
    ("shaped_ssb_heldout.json", shaped_ssb_heldout),
):
    (OUTPUT / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(
    "Shaped SSB definitions: train",
    len(shaped_ssb_train),
    "held-out",
    len(shaped_ssb_heldout),
    "; only one word compiled.",
)
""")

md(r"""
### 17.6 软件波形检查与有约束的候选选择

下面从**最终 DAC**截取已编译 UV channel，独立重算包络与相位并比较到 1 LSB；也核对完整门计数。解析终点 $u=1$ 不属于最后一个样本（实际 $u=n/N$），因此不能强求最后一个活动 DAC 样本恰为零。软件一致不表示波形已到达原子，更不表示 CZ 正确。

右图只是待测的幅度×频率 stress 网格，颜色不是实验结果。对每候选先检查 valid-shot 比例、logical survival、leakage 和 loss 的预设阈值，再比较 closure、echo 与目标相位 Bell 结果；不能把 loss 删掉后靠条件概率挑赢家。各站点分类与帧状态保持同 shot 的联合记录；不同检测设置或实验序列来自不同 shot，不能伪装成联合观测。分母用 valid trials（含 leak/loss），invalid 独立报告；若原始装载也被筛选，必须同时报告装载率与筛选规则。

一个保守选择方法是：只保留在所有 stress 点均满足预先约定 loss/leak/invalid 上限的候选，再按训练 SSB 每 depth/输入态返回率的最差下置信界排序，最后检查目标相位 Bell 的一致性。shot 数、置信水平和阈值在采数前冻结；未取得对应数据时不计算分数。高 pair return 也可能只是 identity，echo 会隐藏部分局域相位，二者都不能单独当目标函数。不能把 SSB 分数、Bell 输出态保真度或旧双方波模型值称为该候选的平均 CZ fidelity。

实际优化先用少量 closure 和 M=1 echo 排除明显失败者；接近目标后再以 M=3、Bell 相位和固定训练 SSB细化。少量 finalist 用**未参与选参**的 seed、重复日期／pair 和幅度／频率 stress 数据验证。任何 RF-only、clock-only 对照都应使用此候选完整门时长重新匹配，不能沿用双方波的 `GATE_TIME_S`。
""")

code("""
# Advanced inspection only: the experiment functions above use public DSL APIs.
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window

assert [point.axis_indices for point in shaped_closure_preview.points] == [
    (0, 0),
    (0, 1),
    (1, 0),
    (1, 1),
]
shaped_sample_receipts = []
for point in shaped_closure_preview.points:
    uv_event = next(
        event
        for event in point.logical_events
        if event.kind == "play" and event.signal == q0.rydberg
    )
    assert np.isclose(
        uv_event.params["amplitude"],
        float(shaped_stress_amplitude.values[point.axis_indices[0]]),
    )
    assert np.isclose(
        uv_event.params["frequency"],
        float(shaped_stress_frequency.values[point.axis_indices[1]]),
    )
    group = next(
        event.params
        for event in point.physical_events
        if event.kind == "spectrum_program"
    )
    program = group["cards"]["B"]["program"]
    uv_record = next(pulse for pulse in program["pulses"] if pulse["channel"] == 1)
    assert uv_record["phase_steps"] == []
    assert uv_record["phase_modulation"]["kind"] == "polynomial"
    np.testing.assert_allclose(
        uv_record["phase_modulation"]["coefficients"],
        shaped_demo_candidate["phase_coefficients_drive_rad"],
        atol=1e-14,
    )
    assert uv_record["duration_samples"] == round(
        shaped_demo_candidate["uv_total_duration_s"] * 1.25e9
    )
    assert abs(uv_record["phase_evolution"]["modulation_end_rad"]) < 1e-12
    assert uv_record["phase_evolution"]["terminal_modulation_phase_rad"] == 0
    n = np.arange(uv_record["duration_samples"])
    u = n / uv_record["duration_samples"]
    shape = uv_record["envelope"]
    edge = shape["sigma_samples"] * shape["cutoff_sigma"]
    distance = np.maximum(edge - np.minimum(n, len(n) - n), 0)
    base = np.exp(-0.5 * shape["cutoff_sigma"] ** 2)
    envelope = (np.exp(-0.5 * (distance / shape["sigma_samples"]) ** 2) - base) / (
        1 - base
    )
    phase_local = sum(
        c * u**i
        for i, c in enumerate(shaped_demo_candidate["phase_coefficients_drive_rad"])
    )
    phase = (
        uv_record["phase_rad"]
        + 2 * np.pi * uv_record["frequency_hz"] * n / 1.25e9
        + uv_record["phase_scale"] * phase_local
    )
    expected_dac = np.rint(32767 * uv_record["amplitude"] * envelope * np.cos(phase))
    start = uv_record["start_sample"]
    uv_column = program["card_spec"]["active_channels"].index(1)
    final_dac = reconstruct_window(program, start, start + len(n) + 1)[:, uv_column]
    np.testing.assert_allclose(final_dac[:-1], expected_dac, atol=1)
    assert final_dac[0] == 0 and final_dac[-1] == 0
    shaped_sample_receipts.append(
        {
            "indices": list(point.axis_indices),
            "sample_count": len(n),
            "max_error_lsb": float(np.max(abs(final_dac[:-1] - expected_dac))),
            "physical_frequency_hz": uv_record["frequency_hz"],
            "physical_peak_amplitude": uv_record["amplitude"],
        }
    )

for preview, complete_gates in (
    (shaped_closure_preview, 1),
    (shaped_echo_preview, 2),
    (shaped_parity_preview, 1),
    (shaped_ssb_preview, ssb_record["total_cz_count"]),
):
    for point in preview.points:
        events = [event for event in point.logical_events if event.kind == "play"]
        assert sum(event.signal == q0.rydberg for event in events) == complete_gates
        assert sum(event.signal == q0.clock for event in events) == 2 * complete_gates
        resets = [
            event for event in point.logical_events if event.kind == "reset_phase"
        ]
        assert len(resets) == 3
        assert all(event.start_mu == events[0].start_mu for event in resets)
    assert not any(
        item["kind"] == "spectrum_modulation_frequency_range"
        for item in preview.diagnostics
    )

fig, axes = plt.subplots(1, 3, figsize=(12, 3))
axes[0].plot(n / 1.25e9 * 1e6, envelope)
axes[0].set(
    xlabel="UV time (us)", ylabel="Peak-normalized envelope", title="Candidate shape"
)
axes[1].plot(n / 1.25e9 * 1e6, phase_local)
axes[1].set(
    xlabel="UV time (us)",
    ylabel="Drive phase modulation (rad)",
    title="One continuous pulse",
)
for i, amplitude_fraction in enumerate((0.98, 1.02)):
    for j, detuning_hz in enumerate(shaped_stress_detuning_hz):
        axes[2].plot(detuning_hz / 1000, amplitude_fraction, "o", color="tab:blue")
        axes[2].annotate(
            f"point {2 * i + j}",
            (detuning_hz / 1000, amplitude_fraction),
            xytext=(4, 4),
            textcoords="offset points",
        )
axes[2].set(
    xlabel="Optical detuning offset (kHz)",
    ylabel="Amplitude / candidate",
    title="Planned stress points; no atom data",
    xlim=(-7, 9),
    ylim=(0.97, 1.035),
)
fig.tight_layout()
fig.savefig(OUTPUT / "shaped_candidate_and_stress_grid.png", dpi=150)
plt.close(fig)
display(Image(filename=str(OUTPUT / "shaped_candidate_and_stress_grid.png")))
(OUTPUT / "shaped_waveform_software_checks.json").write_text(
    json.dumps(
        {
            "scope": "final DAC vs analytic envelope and phase; no hardware",
            "candidate_id": shaped_demo_candidate["candidate_id"],
            "points": shaped_sample_receipts,
        },
        indent=2,
    ),
    encoding="utf-8",
)
""")

md(r"""
## 18. 真实采数表、统计与校准闭环

一个 shot 记录至少包含：run_id、timestamp、实验名、pair/site、zone occupancy、B/trap/温度、配置 revision/hash、扫描值及单位、sequence seed/word/hash、attempt/shot、原始图像引用、0/1/c/r/loss/invalid 分类、分类器版本和后选择规则。保留同 shot 的 site 联合结果。不同检测模式来自不同 shot 时，不能伪装成同 shot 联合分类。

建议交错 reference/信号、不同深度/相位，RF/clock monitor 每隔一批插入。对 binomial 概率使用 Wilson/Beta interval 或 likelihood；fringe/Rabi/decay 正式分析用 counts 与异方差/层级误差。这里的 least-squares 示例只演示可运行的数据通路，不输出假置信区间。

| 更新对象 | 根据什么 | 更新后重做 |
|---|---|---|
| RF frequency | spectroscopy + Ramsey quadrature | 新建实验/编译；检查全局 site 分布 |
| RF π/π2 幅度、时长 | Rabi + ALLXY/误差放大 | 重建命名 pulse → 新实验 → 编译 |
| clock amplitude/time/frequency | transfer + return + optical Ramsey | clock phase 与 return benchmark |
| CZ UV duration/detuning/phase | closure + echo + loss | Bell population/parity + SSB |
| 共同相位补偿 | Bell 相位 + 无 echo SSB training seed | 固定参数后 independent seed |
| zone成员/接线/trigger | 实际配置与搬运记录 | 重建 descriptor/setup/lab，并验证输出覆盖 |

给 Qubit 赋值是用户可编辑入口；不会自动修改已构建的 pulse/Experiment。下面只写**待审阅建议 JSON**，不将 synthetic fit 应用到 q0。真实更新例：`q0.parameters.rf.pi_duration = measured_pi_s`；随后重建对应 pulse 与实验。共享 RF 的独立 site 参数不是独立可执行控制。
""")
code("""
calibration_proposal = {
    "status":"example_only_not_applied", "source":"synthetic/model",
    "rf_pi_duration_s":float(np.pi/fit_rabi[2]),
    "cz_seed_dimensionless":cz_seed,
    "cz_uv_duration_quantized_s":uv_duration_s,
    "cz_complete_gate_time_s":GATE_TIME_S,
    "required_before_application":["raw counts and fit residuals","site uniformity","SPAM/loss/leakage","independent validation seed"],
}
(OUTPUT/"calibration_proposal.example.json").write_text(json.dumps(calibration_proposal,indent=2),encoding="utf-8")
shot_schema_example = {
    "run_id":"EXAMPLE_NO_MEASUREMENTS", "experiment":"ssb", "pair":["q0","q1"],
    "zone_members":["q0","q1"], "rf_affected":["q0","q1","spectator"],
    "total_cz":2, "rf_half_pi_count":33, "initial_state":ssb_record["initial_state"],
    "seed":171,"sequence":0,"shot":0,"attempt":0,
    "outcomes":None,"image_reference":None,"detector_status":"not_connected",
    "postselection_rule":"predeclared; report raw and selected separately",
}
(OUTPUT/"shot_schema.example.json").write_text(json.dumps(shot_schema_example,indent=2),encoding="utf-8")
""")
md(r"""
### 已加入的扩展与仍需实验室接入的项目

二维 RF/clock Ramsey、三种 Rabi chevron、XY8、面积误差放大、人口/clock/UV 返回保持、差分 light shift、CZ closure/echo、输入态损失、SSB 相位和波形候选已在对应章节给出可编译时序。
论文与实现逐项对应见 [benchmarking 与波形优化参考](yb171_benchmarking_references.md)。下表保留需要额外资源的实验，不能用现有 fixture 假装已经采到了数据。

| 实验 | 可执行策略/接入点 | 关键读出 |
|---|---|---|
| B 场与 RF 漂移 | 交错 Ramsey quadrature；标定全局频率斜率 | site 共振分布/时间漂移 |
| transport coherence | 相同 RF Ramsey 外壳，对照 DDS `move` 与等时长静置；按搬运教程声明轨迹、标定、settle 和回程，不伪装 AWG delay | phase、contrast、survival |
| 加热/冷却 | 搬运前后 sideband spectroscopy 或 release-recapture；实际 trap/native 控制另接 | 温度/运动占据、loss |
| RF amplitude noise / spin lock | §6.3 已给时间×幅度扫描；完整噪声谱需另外测响应函数和独立噪声源 | T1ρ 与频谱相关噪声 |
| clock T1/残留 | §11.1 已给三种等时序列，仍需 c 分态探测与 repump 对照 | c survival 与真实 loss |
| Rydberg lifetime | §12.1 已给返回诊断；增加真实 trap/recapture/分支探测才可建寿命模型 | 有限寿命和探测效率 |
| zone 光学串扰 | storage spectator Ramsey；插入 clock-only/UV-only/完整门，匹配时间 | spectator phase/contrast/loss |
| pair 间串扰 | 一对/多对同时 zone 操作，距离扫描 | pair-resolved Bell/SSB、相关误差 |
| AOM transfer function | 光电探测器/异频拍频测 rise/fall、phase jump、频率/幅度耦合 | 原子处光功率/相位时间线 |

搬运、温度、repump、门中探测和光电采集仍需实验室接入。每天的短流程：survival/SPAM → RF Ramsey与面积点 → clock return/phase → Rydberg closure → CZ echo → Bell相位 → 短SSB；漂移超出已定义门限再回到完整 tune-up。门限由实验室目标误差预算确定，不在这里伪造通用数字。
""")
md("""
## 19. 本工作本实际验证与输出

默认编译每个主实验族的少量代表点，完整 RF RB/SSB 门词冻结并验证 ideal recovery；ALLXY 构建全部 21 对，仅代表对进入 Spectrum 编译。pulse/trigger 表来自实际编译结果，不等于实测波形。

输出到 `output/yb171_tuneup/`：两份编译报告、模型/合成图、RF RB/SSB/echo manifest、建议与 shot schema、离线检查摘要。不会覆盖真实数据目录，不连接仪器。全新 kernel 执行、真实 ARTIQ 编译、浏览器渲染、光学同步验收是不同层级。

Run All 后查看 [RF Ramsey 实际编译报告](../../output/yb171_tuneup/rf_ramsey_report/index.html) 和 [CZ echo 实际编译报告](../../output/yb171_tuneup/cz_echo_report/index.html)。报告中 pulse sheet / waveform 来自最终编译样本，不是量子态仿真。
编译 IR 的 `start_mu` 使用 `compiled.setup.tick_seconds`；混合调度 tick 与 ARTIQ ref_period 不一定相同，应换成秒再做物理时长检查。
""")
code("""
# Check critical physical scope/counts on actual compiled IR.
rf_events = [e for e in ramsey_preview.points[0].logical_events if e.kind=="play"]
assert len(rf_events)==2
assert all(e.params["global_operation"]["targets"]==["q0","q1","spectator"] for e in rf_events)
assert all(e.params["frequency"]==31000 for e in rf_events)
echo_events = [e for e in echo_preview.points[1].logical_events if e.kind=="play"]
assert len(echo_events)==3
echo_gaps_s = [(echo_events[i+1].start_mu-echo_events[i].end_mu)*echo_preview.setup.tick_seconds for i in (0,1)]
np.testing.assert_allclose(echo_gaps_s,[100e-6,100e-6],rtol=0,atol=1e-12)
ssb_events = [e for e in ssb_preview.points[0].logical_events if e.kind=="play"]
assert sum(e.signal==q0.rf for e in ssb_events)==33
assert sum(e.signal==q0.clock for e in ssb_events)==2*ssb_record["total_cz_count"]
assert sum(e.signal==q0.rydberg for e in ssb_events)==2*ssb_record["total_cz_count"]
assert all(e.params["global_operation"]["targets"]==["q0","q1"] for e in ssb_events if e.signal in (q0.clock,q0.rydberg))
# New 2D authoring contracts: final frequencies, Cartesian order and edge gaps.
assert [p.axis_indices for p in rf_chevron_preview.points] == [(i,j) for i in range(3) for j in range(3)]
assert len(list(rf_chevron_preview.execution_order())) == 18
for point in rf_chevron_preview.points:
    event = next(e for e in point.logical_events if e.kind=="play")
    assert event.params["frequency"] == point.coordinates["rf_chevron_hz"]
    assert np.isclose(event.duration_mu*rf_chevron_preview.setup.tick_seconds,point.coordinates["rf_chevron_time"])
    assert event.params["global_operation"]["targets"] == ["q0","q1","spectator"]
for point in rf_ramsey_map_preview.points:
    events = [e for e in point.logical_events if e.kind=="play"]
    assert len(events)==2 and sum(e.kind=="reset_phase" for e in point.logical_events)==1
    assert all(e.params["frequency"]==point.coordinates["rf_ramsey_hz"] for e in events)
    assert all(e.params["phase"]==0 for e in events)
    assert np.isclose((events[1].start_mu-events[0].end_mu)*rf_ramsey_map_preview.setup.tick_seconds,point.coordinates["rf_dark_time"])
# Same +1kHz / 10us point as the earlier 1D Ramsey: actual RF carrier phases agree.
old_group = next(e.params for e in ramsey_preview.points[0].physical_events if e.kind=="spectrum_program")
map_group = next(e.params for e in rf_ramsey_map_preview.points[6].physical_events if e.kind=="spectrum_program")
for old_pulse,new_pulse in zip(old_group["cards"]["A"]["program"]["pulses"],map_group["cards"]["A"]["program"]["pulses"],strict=True):
    for field in ("start_sample","duration_samples","frequency_hz","amplitude","phase_rad"):
        assert old_pulse[field] == new_pulse[field]
for light,idle in zip(shelving_map_preview.points,shelving_idle_preview.points,strict=True):
    light_rf = [e for e in light.logical_events if e.kind=="play" and e.signal==q0.rf]
    idle_rf = [e for e in idle.logical_events if e.kind=="play" and e.signal==q0.rf]
    assert [e.start_mu for e in light_rf] == [e.start_mu for e in idle_rf]
    assert light.duration_mu == idle.duration_mu
for preview in (clock_chevron_preview,rydberg_chevron_preview,cz_closure_preview,cz_echo_map_preview):
    for point in preview.points:
        for event in point.logical_events:
            if event.kind=="play" and event.signal in (q0.clock,q0.rydberg):
                assert event.params["global_operation"]["targets"] == ["q0","q1"]
for point in xy8_x_preview.points:
    events = [e for e in point.logical_events if e.kind=="play"]
    np.testing.assert_allclose([e.params["phase"] for e in events[1:-1]],np.deg2rad([0,90,0,90,90,0,90,0]))
    total_dark = sum(events[i+1].start_mu-events[i].end_mu for i in range(len(events)-1))*xy8_x_preview.setup.tick_seconds
    assert np.isclose(total_dark,16*point.coordinates["xy8_half_gap"])
for point in area_pi_preview.points:
    assert sum(e.kind=="play" for e in point.logical_events)==1+2*point.coordinates["area_blocks"]
for point in cz_repetition_preview.points:
    count = point.coordinates["complete_cz_repeats"]
    assert sum(e.kind=="play" and e.signal==q0.clock for e in point.logical_events)==2*count
    assert sum(e.kind=="play" and e.signal==q0.rydberg for e in point.logical_events)==2*count
# 汇总已有编译结果；这里不再次编译，也不执行仪器。
previews = [
    rf_spec_preview, rf_rabi_preview, ramsey_preview, echo_preview, cpmg_preview,
    axis_preview, allxy_preview, amplification_preview, rb_preview,
    clock_spec_preview, clock_rabi_preview, clock_optical_preview,
    clock_phase_preview, idle_phase_preview, return_train_preview, clock_rb_preview,
    rydberg_spec_preview, rydberg_rabi_preview, cz_echo_preview,
    population_preview, parity_preview, *echo_benchmark_previews, ssb_preview,
    rf_chevron_preview, rf_amplitude_preview, rf_ramsey_map_preview, rf_ramsey_phase_preview,
    xy8_x_preview, xy8_y_preview, spin_lock_preview, population_zero_preview, population_one_preview,
    area_pi_preview, area_half_preview, clock_chevron_preview, clock_ramsey_map_preview,
    shelving_map_preview, shelving_idle_preview, stark_preview,
    clock_hold_return_preview, clock_hold_residual_preview, clock_hold_idle_preview,
    rydberg_chevron_preview, rydberg_return_preview, cz_closure_preview, cz_echo_map_preview,
    cz_repetition_preview, cz_loss_zero_preview, cz_loss_plus_preview, cz_loss_one_preview,
    ssb_phase_preview, shaped_closure_preview, shaped_echo_preview, shaped_parity_preview, shaped_ssb_preview,
]
if rb_full_compiled is not None:
    previews.append(rb_full_compiled)
compile_log = [
    {"experiment": result.name, "points": len(result.points),
     "shots": result.shots, "validation": "offline DSL/Spectrum only"}
    for result in previews
]
display(compile_log)
verification = {"scope":"offline previews, ideal algebra, synthetic analysis",
                "compile_log":compile_log,"rf_rb_frozen":len(rb_sequences),"ssb_frozen":len(ssb_records),
                "rf_rb_basis":RB_BASIS_NAME,"rf_rb_preview_conditions":len(rb_preview.points),
                "rf_rb_full_compiled":rb_full_compiled is not None,
                "ssb_rf_half_pi_count":33,"hardware_executed":False,
                "real_artiq_compilation":False,"browser_render_verified":False,
                "measured_quantum_fidelity":None}
(OUTPUT/"offline_verification.json").write_text(json.dumps(verification,indent=2),encoding="utf-8")
print("Offline checks passed. No laboratory measurements were made.")
""")
md(r"""
## 20. 连接真实硬件：另行配置，显式执行

前面的 Run All 到编译和可视化为止。真实 device_db、Spectrum 身份/时钟校准、Qubit 参数和 native 准备/读出来自实验室自己的包；不能把本工作本的占位配置直接切到实机。
下面只展示调用顺序，是 Markdown 示例，不在 Run All 中执行。先用实测参数重建命名 pulse，再构建实验和编译：

```python
real_rf_pi2 = Square(
    duration=calibrated_q0.parameters.rf.pi2_duration,
    amplitude=calibrated_q0.parameters.rf.pi2_amplitude,
)
real_lab = LabConfig(
    setup=real_setup,
    qubits=calibrated_qubits,
    emulation=False,
    artiq_python=artiq_python,
)
real_exp = rf_ramsey(
    calibrated_q0, tau, real_rf_pi2,
    prepare=real_prepare, readout=real_readout, shots=100,
)
real_compiled = real_lab.compile(real_exp)
with real_lab.connect(clock_session_ids=measured_clock_session_ids) as session:
    receipt = session.run(real_compiled)
```

`real_setup` 包含已核对的真实接线与校准，`calibrated_qubits` 必须登记全部受共享输出影响的原子，`artiq_python` 指向实验室独立 ARTIQ 环境。`connect` 核对设备身份与时钟，`run` 才上传并执行；缺少配置时不会静默回退到 emulation。时钟 session 标识须来自当前连续时钟会话，不是随意复制的字符串。

`receipt` 是执行记录，不是原子态或相机图像。相机触发、host 取帧和持久提交是不同步骤；当前 `HardwareSession.run()` 不提供 detector adapter 注入。真实测量接入见[读出 adapter](../readout_adapters.md)，恢复与提交见[持久执行](../streamed_execution.md)。新建独立 MOT/相机组件再读[组件指南](../native_components.md)，无需为了运行这份离线实验先实现组件框架。
""")
md(r"""
## 21. 参考与适用边界

1. [用户提供的 ChatGPT 分享分析](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)：本版两比特实验组织、echo/Bell/SSB 具体方案来源；已读全文，关键理想关系另用矩阵验证。它是设计参考，不替代原论文或实测校准。
2. [Ma et al., Universal Gate Operations on Nuclear Spin Qubits in an Optical Tweezer Array of ¹⁷¹Yb Atoms, PRX 12, 021028 (2022)](https://doi.org/10.1103/PhysRevX.12.021028)：RF 核自旋门、共同照明纠缠脉冲与 Bell 表征。其 Rydberg 激发路径不同于这里的 clock shelving；本版不照搬光学频率与实验参数。
3. [Muniz et al., High-fidelity universal gates in the ¹⁷¹Yb ground state nuclear spin qubit, arXiv:2411.11708](https://arxiv.org/abs/2411.11708)：zone-wide clock/Rydberg 与完整 gate 定义、clock/CZ 对照 benchmarking。该实验的单比特是局域 Raman，因此不能照搬它的完整 2Q Clifford RB 到纯 global RF。
4. [Huie et al., Repetitive Readout and Real-Time Control of Nuclear Spin Qubits in ¹⁷¹Yb Atoms, PRX Quantum 4, 030337 (2023)](https://doi.org/10.1103/PRXQuantum.4.030337)：另一研究组的 ground-state 核自旋 RF、global rotations 与 QND/readout 参考。
5. [Tsai et al., Benchmarking and Fidelity Response Theory of High-Fidelity Rydberg Entangling Gates, PRX Quantum 6, 010331 (2025)](https://doi.org/10.1103/PRXQuantum.6.010331)：SSB 及对称子空间指标、噪声模型响应与解释边界。此处完整 clock 往返是面向本配置的替换，需重新验证误差模型。

维护入口：[notebook 生成器](../../scripts/build_yb171_tuneup_notebook.py)、[可审阅数学辅助](../../examples/yb171_tuneup_analysis.py)、[定向数学验证](../../tests/test_yb171_tuneup_analysis.py)。实验函数直接在 notebook 展示，不依赖隐藏的理想 CZ 代替实际脉冲。
""")

learning_notes = {
    "## 3.": r"""
**原理与学习重点。** 读出是带误判/擦除的测量通道：$p_{obs}=M p_{true}$。只有在 M 的状态集合与真实 leakage/loss 一致、且矩阵能辨识时才可反演。用同一实验批次的空陷阱/已知态建立参考，先检查原始 counts 的分母，再进行校正。

**本节参考：** [Huie et al. 2023，重复读出与 global RF](https://doi.org/10.1103/PRXQuantum.4.030337)；[Muniz et al.，Appendix F 数据分析](https://arxiv.org/html/2411.11708v2#A6)。分类表是本工作本的数据约定，需与实际探测链路对应。
""",
    "## 4.": r"""
**原理。** 在旋转波近似下，失谐 Δ 与驱动 Ω 给出 $\Omega_{eff}=\sqrt{\Omega^2+\Delta^2}$，理想激发概率 $P_1=\Omega^2\sin^2(\Omega_{eff}t/2)/\Omega_{eff}^2$。固定 t 扫 Δ 就得到 spectroscopy；弱驱动长脉冲提高分辨率，但更容易受磁场漂移影响。这里 Ω/Δ 用 rad/s，而扫描轴用 Hz，相差 $2\pi$。

**本节参考：** [Ma et al. 2022，核自旋 RF 控制 §III](https://doi.org/10.1103/PhysRevX.12.021028)；[Huie et al. 2023，ground-state 核自旋控制](https://arxiv.org/abs/2305.02926)。这里的频点、功率和拟合流程是教学设置，不是论文实测参数。
""",
    "## 5.": r"""
**原理。** 共振下 $P_1(t)=\sin^2(\Omega t/2)$，所以 $t_\pi=\pi/\Omega$。实际示例拟合 $a-b e^{-t/T_R}\cos(\Omega t)$；若 off-resonance，则振幅降低、振荡变快，直接把周期当共振 Ω 会偏。先校频率，再校面积，并用重复门放大检查短脉冲边沿误差。T_R 是 Rabi 振荡包络时间，不等同 Ramsey T2*。

**本节参考：** [Ma et al. 2022 §III，RF Rabi 与 coherence](https://doi.org/10.1103/PhysRevX.12.021028)。衰减包络形式是本版可替换的 phenomenological fit，应由残差决定是否适合。
""",
    "## 6.": r"""
**Ramsey 原理。** 首 π/2 将纵向人口转为横向相干，暗时间累积相位；末 π/2 将相位转为人口。常用模型 $P_1=a+b\exp[-(\tau/T_2^*)^\alpha]\cos(2\pi\delta f\tau+\phi)$。固定一个 quadrature 时，不能总由 cos 的频率符号判断失谐方向。

**Echo 原理。** 中间 π 把两半时段的静态 detuning 相位反号，抑制 quasi-static 展宽，保留较快噪声和 pulse error。拟合横轴是总暗时间 2τ；echo 与 Ramsey 不必服从同一包络指数。

**CPMG 原理。** 首 X90 产生沿 −Y 的磁化，后续 Y180 围绕磁化轴 refocus，使脉冲面积误差不易累积；末 −X90 读出。多脉冲改变噪声滤波函数，测到的衰减不是不依赖序列的单一材料常数。

**本节参考：** [Ma et al. 2022，核自旋相干](https://doi.org/10.1103/PhysRevX.12.021028)；[Hahn, Spin Echoes (1950)](https://doi.org/10.1103/PhysRev.80.580)；[Meiboom & Gill, Modified Spin-Echo Method (1958)，原文](https://122.physics.ucdavis.edu/sites/default/files/files/Pulse%20NMR/Meiboom_Gill_RevSciInstr.29.6881_1958.pdf)。频率锁定建议是本工作本的接入设计。
""",
    "## 7.": r"""
**原理。** 理想旋转为 $R_\phi(\theta)$；不同顺序的 X/Y 非对易，因此同样的面积、失谐、轴相位误差会在不同门对中产生不同的残差图样。ALLXY 把这些响应排列为易比较的阶梯，重复门则把小 coherent 误差放大。诊断图样不是唯一反演，须结合 spectroscopy/Ramsey 交叉判断。

**本节参考：** [Matthew Reed, Yale PhD thesis (2013)，pulse tune-up 与 AllXY](https://rsl.yale.edu/sites/default/files/2024-08/2013-RSL-Thesis-Matthew-Reed.pdf)。原工作是超导 microwave qubit；此处复用两能级旋转代数，改为全局 RF，不照搬其硬件误差模型。
""",
    "## 8.": r"""
**原理。** Clifford 随机化把许多方向相关误差平均成可用少量参数描述的衰减；理想 recovery 把无误差终态送回初态。SPAM 通常进入 A/B，误差随 Clifford 数累积进入 p，但明显 gate dependence、相关噪声和 leakage 会破坏简单单指数解释。

**本节参考：** [Magesan, Gambetta & Emerson, PRL 106, 180504 (2011)](https://doi.org/10.1103/PhysRevLett.106.180504)；[Ma et al. 2022，RF 平台](https://doi.org/10.1103/PhysRevX.12.021028)。本节选择相同序列广播，并保留 site 统计；没有假设局域 RF。
""",
    "## 9.": r"""
**Clock spectroscopy 原理。** 用窄 $|1\rangle\leftrightarrow|c\rangle$ 跃迁把激光失谐变为转移概率；陷阱差分光移、Doppler/边带与 Zeeman 结构都可进入谱线。不同初始核自旋态的对照检查 state-selectivity。

**Clock length Rabi 原理。** 与 RF 的两能级旋转形式相同，但这里相位参考来自光学链路，且 c-state 陷阱/寿命/回收会影响观测。单次转移最大与完整核自旋相干往返最好不一定重合。

**本节参考：** [Muniz et al. §IV.1 / Appendix C，clock shelving](https://arxiv.org/html/2411.11708v2)。本版的 IF 频率和方波只是起点，不能声称复现论文脉冲整形和实测 transfer。
""",
    "## 10.": r"""
**相位如何产生。** 两次理想 clock π 对 $|1\rangle$ 的幅度因子为 $-e^{i(\phi_{in}-\phi_{out})}$（本版旋转约定）；与未耦合的 |0〉比较才看见逻辑相位。RF Ramsey 外壳测这个相对相位；optical Ramsey 则测 |1〉与 |c〉的相干。二者状态空间不同，不能互相替代。扫相位时至少覆盖一个完整周期，并同时拟合 cos/sin quadrature。

**本节参考：** [Muniz et al. clock operations / two-qubit calibration](https://arxiv.org/html/2411.11708v2)；[分享分析中的完整往返定义与共同相位](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)。负号和代码相位约定在本工作本独立推导。
""",
    "## 11.": r"""
**为什么做三个 benchmark。** population return 检查是否回到逻辑态，却对确定性相位不敏感；RF 包裹的 return train 将相位/退相干转为 fringe；optical Clifford RB 平均光学 Bloch 球上的控制误差，却不直接代表 ground-spin 的完整往返。重复次数是被测操作次数，不能将 RF 外壳或 SPAM 的误差随意除掉。

**本节参考：** [Muniz et al.，clock 及 entangling gate 的分层 benchmark](https://arxiv.org/html/2411.11708v2)；[Magesan et al.，RB 原理](https://doi.org/10.1103/PhysRevLett.106.180504)。optical 1Q RB 是本版可选拓展，不声称论文以相同 RF 编码执行了它。
""",
    "## 12.": r"""
**原理。** 孤立原子 c↔r 满足受驱两能级模型；两原子的 rr 能级被相互作用 V 移位。强 blockade 抑制 rr，cc 与对称单激发态 $(cr+rc)/\sqrt2$ 以 $\sqrt2\Omega$ 耦合。比较单原子与 pair 的谱、振荡和双激发概率，才能区分 Ω 漂移与 blockade。损失还含反俘获失败，因此 loss≠P(r)。

**本节参考：** [Muniz et al. §IV.2 / Appendix D，clock→Rydberg](https://arxiv.org/html/2411.11708v2)；[Ma et al. 2022 §IV，blockade 与共同照明](https://doi.org/10.1103/PhysRevX.12.021028)。后者激发路径不同，只借鉴集体动力学与校准逻辑。
""",
    "## 13.": r"""
**本节参考：** [Ma et al. 2022 §IV，共同两段失谐 pulse](https://doi.org/10.1103/PhysRevX.12.021028)；[Muniz et al. §IV，clock-shelved ground-spin CZ](https://arxiv.org/html/2411.11708v2)。本版将两段 UV 模型与完整 clock 往返组合，模型 seed 与实际完整门刻意分开。
""",
    "### 13.1": r"""
**本节参考：** [Muniz et al. Appendix E/G，CZ 校准及误差预算](https://arxiv.org/html/2411.11708v2)；有限 blockade 图来自本工作本 Hamiltonian，不是复制的论文数据。
""",
    "## 14.": r"""
**本节参考：** [分享分析，第一项全局回波相位标定](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)；[Muniz et al. Appendix E，重复门相位优化](https://arxiv.org/html/2411.11708v2)。下面矩阵断言验证的是该对角模型恒等式，不证明硬件满足对角假设。
""",
    "## 15.": r"""
**本节参考：** [分享分析，第二项 Bell/宇称](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)；[Ma et al. 2022 Fig. 4，Bell 人口及宇称表征](https://doi.org/10.1103/PhysRevX.12.021028)。本版明确目标 β=π/2，与其它旋转符号下的 Bell 相位不要混用。
""",
    "### 可选扩展": r"""
**来源说明：** 该辨识公式为本工作本对上述 diagonal model 的推导；模型背景见[分享分析](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)，不将它归称为原论文已执行的实验。
""",
    "## 16.": r"""
**本节参考：** [Muniz et al. §IV.3，U-GERB 及其敏感性限制](https://arxiv.org/html/2411.11708v2)；[分享分析，第三项随机回波对照](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)。这里随机集合、恢复词与拟合是可审阅的简化实现，不冒称严格 Haar U-GERB。
""",
    "## 17.": r"""
**本节参考：** [Tsai et al. Fig. 2 / Appendix E，12 态与固定模板](https://arxiv.org/html/2407.20184v2#A5)；[分享分析，第四项无回波 SSB](https://chatgpt.com/s/t_6afaf587d0ec8191bce5f5f8857cde4a)。固定 RF 数减少随 CZ 深度增长的 RF 开销混淆，但不消除任意相关噪声偏差。
""",
    "### 17.1": r"""
**本节参考：** [Tsai et al. §II / Appendix A/E/F，SSB 拟合及 fidelity response](https://arxiv.org/html/2407.20184v2)。论文使用自己的 likelihood 与读出修正；下面只演示 synthetic 最小二乘，不照搬 Sr 的 leakage 修正到 Yb。
""",
    "### 已加入的扩展": r"""
**扩展实验来源：** [Ma et al. 2022，RF coherence](https://doi.org/10.1103/PhysRevX.12.021028)；[Huie et al. 2023，重复探测](https://doi.org/10.1103/PRXQuantum.4.030337)；[Muniz et al.，zone/clock/Rydberg 与误差预算](https://arxiv.org/html/2411.11708v2)。搬运、AOM 探测和 daily check 的具体接入是本工作本的实验工程建议，需按真实仪器实现。
""",
}
for cell in cells:
    if cell.cell_type == "markdown":
        for prefix, note in learning_notes.items():
            if cell.source.startswith(prefix):
                cell.source += "\n\n" + textwrap.dedent(note).strip()
                break

for index, cell in enumerate(cells):
    cell.id = f"yb171-tuneup-{index:03d}"
notebook = nbformat.v4.new_notebook(cells=cells)
notebook.metadata.kernelspec = {
    "display_name": "Python 3 (project)",
    "language": "python",
    "name": "python3",
}
notebook.metadata.language_info = {"name": "python", "version": "3.12"}
nbformat.validate(notebook)
destination = ROOT / "docs/tutorials/yb171_global_rf_tuneup.ipynb"
nbformat.write(notebook, destination)
subprocess.run(
    [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        "I,F401",
        "--fix",
        str(destination),
    ],
    check=True,
)
subprocess.run([sys.executable, "-m", "ruff", "format", str(destination)], check=True)
print(destination)
