# Spectrum 的载波与调制相位

Spectrum 默认以每个 `triggered_sequence` 的入口为载波零相位参考，
前级 MOT、冷却和等待的长度不会改变独立门序列的样本。`phase_modulation`
默认只作用于当前 pulse；变频后的连续演化和调制相位继承分别由 `continuous`
和 `carry` 选择。这些选择相互独立。

## 统一 frame 与载波参考

只有一个持久驱动 frame F。`shift_phase(frame, delta)` 加 delta，
`Rz(q, theta)` 减 theta。Rz 保留门级指令来源，但底层使用同一相位更新机制。
`reset_frame(frame)` 清整个 F，包括 Rz 贡献，不改变振荡器相位和调制继承。
`reset_phase(frame)` 同时清 F、清继承调制，并在该指令的最终排程位置建立零载波参考。
它不抹掉 pulse 自己的 phase 和固定校准，也不制备或复位量子态。

每个 shot 从 F=0 开始。默认的 sequence 入口只重新建立实际输出的载波和调制
参考，不清掉入口前显式编写的 Rz/shift。因此首门的完整相位仍包括这些操作、
pulse 自身的 phase 和校准，不保证首个 DAC 数值为零。

需要承接前级相干控制的共同时间参考时，显式使用
`exp.triggered_sequence(group="qubit_awg", phase_reference="shot")`。
这保留 shot T0 参考（或此前最后一个显式 reset_phase），不表示跨 shot 继承。
Section、operation、chunk、segment 和 tile 的边界都不复位相位。
通常无需在独立 AWG 序列开头重复写 reset_phase；只有希望连显式 frame 更新也
清除，或在序列内部重新定相时才需要。reset_phase 不允许打断活动 pulse。

## 载波模式属于实际输出配置

```python
SpectrumOutput("A", 0)  # 默认 carrier_mode="epoch"

SpectrumOutput(
    "A",
    0,
    carrier_mode="continuous",
    initial_frequency_hz=7e6,
)
```

`initial_frequency_hz` 是物理输出频率，不再经过逻辑 signal 的 frequency_scale
或 frequency_offset。continuous 模式必须填写；epoch 模式不接受这个无作用的值。

设门开始于 t_i，实际载频为 f_i，当前载波参考时刻为 Tref：默认是 sequence
入口；shot 模式是 shot T0；显式 reset_phase 后则为该指令时刻。

- **epoch**：该门的载波相位为 `C(t)=2π f_i (t−Tref)`。各频率参考同一个 Tref。
- **continuous**：`C(Tref)=0`，随后按实际频率逐段积分。首门前按声明的初始频率
  推进；门开始时切换到该门的物理载频；门后的等待仍沿用上一频率。
  reset_phase 不改变当前频率，只重新建立相位参考。

固定频率时两者可以给出相同结果。变频时通常不同：epoch 是对共同时间参考
指定每门相位，continuous 是让同一个振荡器改变频率后继续运行。

状态按实际输出区分：两个逻辑 alias 如果映射到同一输出，会使用同一个 C 和 M；
卡上不同 channel 的状态互相独立。零时长 play 不播放，也不修改频率或 M。

## 调制模式属于 pulse，可在 play 调用处覆盖

```python
pulse = Square(
    400 * ns,
    frequency=11e6,
    phase_modulation=PolynomialPhase((0.2, 0.7)),
    phase_modulation_mode="carry",
)
play(q.rf, pulse)
play(q.rf, pulse, phase_modulation_mode="local")
```

设局部坐标 `u=(t−t_i)/T`，调制相位为 m(u)，binding 相位比例为 s、偏移为 b，
校准相位为 c，pulse 基础 phase 为 p，最终相位为：

```text
Phi_osc(t) = C(t) + M
phi_pulse(u) = p + m(u)
θ(t) = Phi_osc(t) + s [F(t) + phi_pulse(u)] + b + c
RF(t) = amplitude × envelope(t) × cos(θ(t))
```

- **local**：当前门使用 m(u)，门结束后不修改 M。
- **carry**：当前门使用同样的 m(u)，门结束后 `M ← M + s m(1)`。

因此只切换某个 pulse 的 local/carry，不改变这个 pulse 的样本，只改变之后
脉冲的状态。M 已经是实际输出上的相位，下一门不会再用自己的 s 把它缩放一次。
p、b、c 不随 carry 累加。s 对应 ControlBinding.phase_scale，默认 1；
反号或比例关系由实际映射决定，不应仅因改变载频就随意修改 s。
M 按实际输出共享，F 按控制 frame 共享，两者不应混为一个全局相位变量。

### 为什么 carry 使用完整终点 m(1)

在调制光滑的区间，瞬时频率是：

```text
f_inst = f_i + s m'(u)/(2πT)
平滑部分的额外相位积分 = s [m(1) − m(0)]
```

现有波形还保留 m(0) 对应的入口相位跳变。例子 `m(u)=0.2+0.7u` 先在入口
增加 0.2 rad，再在门内增加 0.7 rad，所以完整结束值为 0.9 rad。carry 继承
0.9 rad；只继承 0.7 rad 会在出口额外丢掉入口的 0.2 rad。

这里没有再次积分并叠加 m(u)：门内样本本来已包含它，M 只在门结束时更新一次。
`HalfSinePhase(a)` 的解析起终值均为零，carry 的净更新为零。

终点使用解析边界 u=1。实际 N 个样本覆盖 `n=0,...,N−1`，使用 `u=n/N`；
不能拿最后一个采样点代替解析终点，否则继承量会随采样率或门长改变。

## Rz、执行域与相位求值顺序

正 `Rz(theta)` 使 F 减少 theta，和 `shift_phase(frame, -theta)` 使用相同
驱动相位更新。`reset_frame` 和 `reset_phase` 都清除其贡献。Rz 仍保留独立
门级 IR；来源信息不能再次从 F 中扣除。

如果一个 qubit 的关联驱动都属于同一个 Spectrum 组，可以在外层 realtime
串行位置写 Rz，然后在 triggered_sequence 中调用普通 play：

```python
with exp.realtime():
    rz(q, 0.3)
    with exp.triggered_sequence(group="qubit_awg"):
        play(q.rf, pulse)
    rz(q, 0.7)
```

第一个 Rz 进入 AWG 门的 V；末尾 Rz 进入 shot 的最终逻辑 frame，不回头改变
已经播放的波形。块外这类 Rz 不生成 DDS 命令。`compiled.frame_phases()`
显示最终 F；`compiled.virtual_z_frames()` 仅显示最近一次 reset 后的 Rz 来源贡献，
不再代表第二个运行 frame。region 的 terminal_frames 显示该 region 退出时的状态。
同组纯 Spectrum frame 也支持块外串行 shift/reset。同 qubit 同时关联 DDS 与 AWG、外部并行分支
中的跨域 Rz、同组每 shot 多次独立启动不属于此扩展。

编译器先确定最终时序，再解析 F，然后解析实际输出载波/调制、映射和校准，最后
生成 DAC 样本。ASAP 改变开始时间后，载波必须按最终时间计算。分段、零等待
压缩、驻留复用和分块上传不能重复累计 carry。

## 诊断的范围与硬件边界

每个导出的 pulse 保留 `phase_evolution`：载波模式及起终相位、进入与退出的 M、
调制解析起终值、入口/出口跳变、光滑部分的瞬时频率范围。曲线超过配置的名义
频率范围时生成 `spectrum_modulation_frequency_range` 诊断，不偷偷改变曲线。

名义载频仍须满足输出配置范围；解析端点、积分和样本必须有限。瞬时频率诊断
不是模拟带宽证明：有限包络和相位跳变仍可能产生额外频谱成分。真实的输出响应、
两卡时钟/触发同步、线缆延迟与相位校准需要各自的实测验收。

纯 ARTIQ 的 reset_frame 同样清整个 F。reset_phase 按已知 DDS 相位状态补偿
到新的参考点，具体更新依赖 timed Square、ASF RAM 或 POWASF 等路径的准备与
提交约束；不是简单地把 POW 写成零。continuous NCO 仍要求每 DDS 每 shot
固定 FTW，不因此获得 Spectrum 的调制 API 或 shot 内连续变频能力。

## 离线对照示例

```powershell
uv run --locked python examples/artiq_spectrum/phase_modes.py
uv run --locked python examples/artiq_spectrum/phase_modes.py --reports
```

第一条只编译四种组合并写入 `output/artiq_spectrum/phase_modes/phase_comparison.json`；
第二条另外生成四份完整报告。示例复制模拟配置，不修改公共 profile、不连接仪器。
第二门展示 reset_frame 保留载波推进和 carry；第三门展示 reset_phase 清掉
这些历史并从新的零载波参考开始，两个 reset 都清 F。
