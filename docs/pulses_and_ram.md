# Pulse 参数与 AD9910 RAM 包络

backend 7 新增 [RAM 门边界相位与连续重放](ram_boundary_timing.md)：同驻留包络、
同 FTW 可在前门期间预写 POW，再按两条独立输出延迟提交；有相位变化时必须证明
原有零幅度尾部覆盖管线差。不插 gap，也不保证任意包络均能连续播放。
下文 RF-off 快速路径的 592/112 ns 间隔只适用于未配置 `ram_phase_latency_mu` 的旧模式。

`play()` 的显式参数只覆盖本次调用，不修改 pulse。`amplitude` 是绝对峰值，
`phase` 单位为 rad，随后叠加当前 frame 相位；不是对 pulse 原值再乘倍率或再加偏移。
`frequency` 单位为 Hz。未覆盖的字段继续使用 pulse 默认值，再按既有规则读取 qubit 默认值。
Square 使用 `duration`，FlatTopGaussian 使用 `flat_duration`，二者不混用。

```python
from dataclasses import replace

from taiyidsl import (
    Experiment,
    FlatTopGaussian,
    Qubit,
    SweepParameter,
    play,
    reference_lab,
    us,
)

q = Qubit("q0")
flat = SweepParameter("flat_duration", [0, 4, 8], unit="us")
pulse = FlatTopGaussian(flat_duration=flat, sigma=0.5 * us, cutoff_sigma=4)
exp = Experiment("flat_top_scan", [q])
with exp.average(100):
    with exp.sweep(flat):
        with exp.realtime():
            play(q.raman, pulse, amplitude=0.5, phase=0.2)

lab = reference_lab([q])
# 64 mu 是离线示例的显式时序假设，不是测量值，也不是芯片固定延迟。
device = lab.setup.devices["urukul0_ch0"]
lab.setup.devices[device.name] = replace(device, ram_latency_mu=64)
compiled = lab.compile(exp)
```

`flat_duration`、幅度、频率和相位支持静态 sweep；`sigma`、`cutoff_sigma` 和
`sample_interval` 保持静态。零平台时长保留两个高斯边沿，不表示零脉冲。

## 包络定义

单侧边沿长 `edge = cutoff_sigma * sigma`；总时长为
`flat_duration + 2 * edge`。平台归一化为 1。在平台外，令 `d` 为到最近平台边界的距离：

```text
envelope = (exp(-d² / (2 sigma²)) - exp(-cutoff_sigma² / 2))
           / (1 - exp(-cutoff_sigma² / 2))
```

这是平移并归一化的截断高斯，两个端点严格为零。编译器将该包络乘以本次调用的
有效幅度、信号映射比例和硬件幅度标定，再量化成 14-bit ASF；不会假设 RAM 数据
还能被单音幅度寄存器再次缩放。

## 采样与资源限制

- 默认 `sample_interval=20*ns`。必须能表示成 `4*step/sysclk`，且 `step` 为 1..65535。
- 总时长与单侧边沿时长必须整除采样间隔；每个边沿至少包含两个采样间隔。
- RAM 数组包含最终的零样本，长度为 `total_duration/sample_interval + 1`，最多 1024。
  不足时明确报错，用户可以显式选择更粗的采样间隔；编译器不自行改变分辨率。
- 同一个 DDS 可以在一个 shot 内重复播放驻留波形。每 DDS 每点最多 8 个 RAM
  profile，共用 1024 字；不同扫描点/shot 开始前重新预加载，不在门之间写 RAM。
- 波形库按**实际量化后的 ASF 数组与采样步长**去重。相位和载频不进入波形身份；
  幅度覆盖先应用信号/硬件标定再采样，因此不同峰值通常占用不同波形。
- RAM 所在 IO_UPDATE 组仍不允许其他活动 DDS；同一 DDS 同一 shot 混用 Square
  和 RAM 尚不支持。不同扫描点可以切换，两种模式之间恢复相应寄存器配置。
- RAM 启动占用 8 mu 提交窗口，重装配占用完整声明的准备窗口；检查共享总线、
  全局提交、输出边沿和显式 `reserve()`，不偷偷调整逻辑时序。

## 长门序列：预存并重复调用

完整可运行示例是 [`examples/ram_gate_sequence.py`](../examples/ram_gate_sequence.py)。
其核心写法如下；下面幅度仅演示数据流，真实 π、π/2 门需要各自的 Rabi 标定。

```python
pulse = FlatTopGaussian(4 * us, sigma=0.5 * us)
gap = 4112 * ns
with exp.average(1):
    with exp.realtime():
        with exp.repeat(256):
            play(q.raman, pulse, amplitude=0.5, phase=0)  # X
            delay(q.raman, gap)
            play(q.raman, pulse, amplitude=0.5, phase=math.pi / 2)  # Y
            delay(q.raman, gap)
            play(q.raman, pulse, amplitude=0.25, phase=0)  # X/2 标签
            delay(q.raman, gap)
            play(q.raman, pulse, amplitude=0.25, phase=math.pi / 2)  # Y/2 标签
            delay(q.raman, gap)
```

这个 motif 明确在最后一门后也保留一次 gap。它产生 1024 次播放，但仅有两个
401 字包络，共 **802 RAM 字**。X/Y 共用第一个 profile，X/2/Y/2 共用第二个；
逐次相位通过独立 POW 寄存器写入，并应用 frame 与 shot reference 补偿。
不假设 RAM profile 本身带可自由调节的幅度乘法器。

`amplitude` 是单次绝对峰值，模板不变。很多不同的幅度/长度/采样步长可能使波形库
超出 8 profile 或 1024 字，这时明确报错。当前没有中途换入/换出波形的机制。
RF 幅度减半或平台时长减半不自动保证旋转角减半，特别是 Raman 耦合和 Gaussian 两翼
都需要纳入实际标定。

默认 `auto` 导出对重复 RAM 使用 core 内存中的事件表和运行时循环，kernel 指令体
不随门数展开；`mode="unrolled"` 保留逐事件源码用于调试。两者都是逐扫描点 kernel，
manifest 的 `ram_execution` 区分 `event_table` 与 `unrolled`，`ram_table_points`
给出使用事件表的点。事件表存的是时间、opcode、寄存器字和 profile 编号，不是波形样本。
DDS RAM 占用不随重复门数增长；core 数据内存、传输量、编译器展开预算与 RTIO 吞吐
仍然有限，不能把它理解成无限序列能力。

## 每次调用的准备过程与最小门间隔

配置中显式提供：

```python
lab.setup.devices[name] = replace(
    lab.setup.devices[name],
    ram_rearm_duration_mu=4000,
    ram_latency_mu=112,
)
```

`ram_rearm_duration_mu` 默认为 None。配置这个字段表示采用可重复调用的 RAM 模式，
即使某个扫描点只有一门也保持 `matched_latency_enable=1`，避免扫描时切换管线模式。
需要单独校准该模式的 `ram_latency_mu`；代码要求其至少覆盖 106 个 DDS 系统时钟周期，
并满足配置栅格。示例 112 mu 是 1 GHz DDS、1 ns/mu 的对齐假设，不是测量值。
未配置 rearm 字段时保留原单包络、matched-off 行为，不允许重复调用。

第一门的寄存器准备在 shot 原点之前完成。未开启下述快速路径时，后续调用在 RF 已关闭的窗口内：

1. 关闭 RAM 并发出 IO_UPDATE，使 profile 切换不会提前触发包络。
2. 选择已驻留的 profile，写 FTW 和本次 POW；在 RAM 关闭时提交这些参数。
3. 暂存 RAM enable/phase-autoclear，在计划的 launch 时刻再发出 IO_UPDATE。
4. 等待声明的输出管线延迟，在逻辑脉冲起点打开 RF，完整包络结束后关闭。

以**上一门 RF 关闭到下一门 RF 打开**定义门间隔，完整重装配路径要求：

```text
min_gap_mu = ram_rearm_duration_mu + ram_latency_mu
```

还必须满足时序栅格和其他资源约束。示例为 **4000 + 112 = 4112 mu = 4.112 μs**；
8 μs 包络的开始到开始间隔为 12.112 μs。紧邻、零间隔或短于契约的调用会报错，
不会自动增加 delay。未配置快速路径时，相同 profile 也执行这条完整重装配路径。

已用本机 ARTIQ `9.9664+071b41b` 的真实驱动方法体核对游标推进：在 coarse=8 mu、
1 ns/mu 时，Urukul Rev9 为 **3440 mu**，Rev8 为 **2960 mu**，不含最后的启动脉冲。
公式分别是 `428*C+16` 和 `368*C+16`，C 为 SPI coarse period（mu）。host 检查声明
预算是否低于实际驱动配置的这个下限，kernel 还检查实际游标是否超出声明预算。
4 μs 留出的预算不是实机 CPU/RTIO 保障；更改版本或时钟后应重新运行审计并验收。
若将 Rev9 准备预算设为无余量的 3440 mu，配置模型允许的下限是
**3440 + 112 = 3552 mu = 3.552 μs**；示例选择 4.112 μs 留出 560 ns 准备余量。
这两个数字都不代表已实测的无 underflow 最短门间隔。

```powershell
.\scripts\artiq-python.ps1 examples/artiq_ram_timing.py --proto-rev 9 --output output/ram_replay/driver_timing_rev9.json
```

这是隔离了 RTIO 调用的**真实驱动 Python 游标模型**，不是 FPGA 仿真或硬件测量。

## 同波形、同频率的快速重放

2026-11-02 起可显式配置 `ram_phase_update_duration_mu`，开启同 profile、同量化
FTW 的快速路径。已有 rearm 和 matched-latency 契约仍需提供：

```python
lab.setup.devices[name] = replace(
    lab.setup.devices[name],
    ram_rearm_duration_mu=4000,
    ram_phase_update_duration_mu=480,
    ram_latency_mu=112,
)
```

AD9910 RAMPUP 在每次 IO_UPDATE 时都从起始地址重放，所以两种快速路径都保持
RAM enabled，且不再写 CFR1、FTW 或选择 profile：

| 相邻调用的实际硬件参数 | 门间操作 | 示例最小 RF-off 间隔 |
|---|---|---:|
| 同 profile、FTW，POW 改变（通常为 X/Y 切换） | `set_pow`，然后一次启动 IO_UPDATE | 480 + 112 = **592 ns** |
| 同 profile、FTW、POW | 仅启动 IO_UPDATE | **112 ns** |
| profile 或 FTW 改变 | 完整 rearm | 4000 + 112 = **4112 ns** |

这里按 1 ns/mu、DDS 1 GHz、SPI coarse=8 mu 计算。真实 ARTIQ 9 驱动的
`set_pow` 游标推进为 `52*C`，此处是 **416 ns**，与 Urukul Rev8/Rev9 无关。
相位准备预算设为无余量的 416 mu 时，模型允许 **528 ns** 间隔；示例使用480 mu。
最终 IO_UPDATE 高电平持续8 mu，包含在112 mu启动到输出窗口中，不需再次相加。
host 检查实际 SPI coarse 对应的预算下限，kernel 检查驱动游标是否越过声明窗口。

“完全相同门”必须按**补偿后的实际 POW**判断。当前保持 `phase_autoclear=1`，
以每次启动相对 shot 原点的时间补偿相位；同一个 `phase=0` 在不同时间可能需要
不同 POW。此时仍只写 POW，无需关闭 RAM。不能为了省 SPI 而改变相位参考。
示例特意用1 GHz下可精确表示的125 MHz和8 ns栅格，使重复门的实际POW相同；
任意频率或间隔不保证满足这个条件。X/Y 两个相位仍共用同一个幅度波形。

完整例子 [`examples/ram_fast_replay.py`](../examples/ram_fast_replay.py) 分别生成
1024门 X/Y 交替和相同门重复序列，每种只占401 RAM字；另导出8门unrolled用于对照。
`ram_replay_action` 记录 first/rearm/phase/retrigger，`dds_ram_phase` 记录相位写入窗口。
manifest 的 `replay_actions` 和 `min_gap_by_action_mu` 给出实际路径计数和间隔要求；
`min_gap_mu` 是该点后续调用所需间隔的最大值（单门时保留重装配契约值）。

```powershell
uv run --locked python examples/ram_fast_replay.py
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_fast_replay/xy --target rv32g --output output/artiq_compile/ram_fast_xy
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_fast_replay/repeat --target rv32g --output output/artiq_compile/ram_fast_repeat
```

这些间隔是保守的软件排程契约，尚未测量实机 RTIO underflow、RF 开关或光学相位。
当前将 POW 写入也放在上一门 RF 关闭后；虽然芯片寄存器有缓冲，未来可研究在前门
播放期间预写下一门 POW，以隐藏 SPI 时间，但须防止中间 IO_UPDATE 提前提交。
此旧模式不提前写入；新模式见 [RAM 边界时序](ram_boundary_timing.md)。112 ns
不是芯片或实验的绝对极限。

## 准备、启动和光学时序

RAM 波形与 profile 在计时原点之前加载，RF 开关保持关闭。存档中的
`dds_ram_start` 包含实际 ASF 数组、采样步长、profile/地址和相位字；后续调用还保留
`dds_ram_rearm` 或 `dds_ram_phase` 准备区间，纯重触发没有准备事件。
每次调用都可从物理计划追溯回逻辑 play。

必须显式提供 `Device.ram_latency_mu`，表示此硬件配置下从 RAM IO_UPDATE 到包络
输出的时序约定，默认 `None` 会拒绝 Gaussian 编译。它与通用输出路径 `latency_mu`
不同。当前要求至少 8 mu 且与 DDS 配置栅格对齐；不满足时拒绝，不做隐式取整。
编译器提前 `ram_latency_mu` 发出 IO_UPDATE，在预定包络起点打开 RF，完整包络结束后关闭。
预加载还会提交零幅度，并按单包络或重复调用契约设置 matched-latency 模式，避免
RAM profile 暂时被当作单音 profile 时产生残留幅度。切回 Square 扫描点时恢复 profile 7 与单音幅度源。

导出的 host 检查要求直接 TTL IO_UPDATE、ARTIQ 9 的 `set_profile(channel, profile)`
接口、独立 DDS chip select、以及零 `sync_data.io_update_delay`；SPI RegIOUpdate 和
非零 update-delay 校准目前明确拒绝。设备初始化后会再次检查 EEPROM 加载的校准。
IO_UPDATE 时执行相位累加器清零，并使用实际启动时刻相对 shot 参考的相位补偿。

mock 检查预加载内容、实际驱动调用、启动到 RF gate 的间隔、包络完整时长、资源
约束、扫描索引和 Square/RAM 状态切换。它不证明真实 ARTIQ 编译通过，也不证明
RTIO、DAC 管线延迟、RF 相位或 AOM 后光场已验证。实机使用前必须按所选芯片模式
实测这条时序与相位约定；不能把示例 64/112 mu 当作测量校准。

2026-10-31：1024 门事件表版本和 8 门调试展开版本均已通过真实 ARTIQ 9 / rv32g
编译；报告分别在 `output/artiq_compile/ram_gate_sequence/` 和
`output/artiq_compile/ram_gate_sequence_unrolled/`。没有执行 kernel 或连接仪器。

```powershell
uv run --locked python examples/ram_gate_sequence.py
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_gate_sequence/artiq --target rv32g --output output/artiq_compile/ram_gate_sequence
```

参考：[ARTIQ AD9910 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html)、
[Urukul 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)、
[AD9910 数据手册](https://www.analog.com/media/en/technical-documentation/data-sheets/ad9910.pdf)。
