# RAM 门边界相位与连续重放

> 本页旧零尾/autoclear 规则属于 legacy_tracking。Backend 9 continuous 路径
> 使用严格相位边界，800 ns 换轴也必须满足幅相响应对齐；参见
> [当前连续 NCO 契约](continuous_nco.md)。

2026-11-06更新：默认验收门长改为800ns和200ns；前者单板声明模型可通过，后者
换相仍拒绝，双独立板800ns当前也因准备串行化拒绝。完整区分硬件与实现限制见
[短门相位与virtual Z审查](phase_switching_and_virtual_z.md)。下述联合载波响应仍是
未测量假设，尚不能据此声称实机无毛刺。

backend 7 支持同一驻留 ASF 包络、同一量化载频的连续重放。实验仍按普通顺序书写：

```python
play(q.raman, envelope)
shift_phase(q.frame, math.pi / 2)
play(q.raman, envelope)
reset_frame(q.frame)
play(q.raman, envelope)
```

三个门共用一份 RAM 数据。后一个门的 POW 可在前一门期间预写，计划中的 IO_UPDATE
才提交并重启包络；相邻门之间 RF switch 保持开启。不会添加 delay、补零、截短包络、
降低采样率或将整段序列拼进 RAM。独立 IO_UPDATE 组可同刻启动和提交，SPI 准备按
全局提交资源排程；容量或准备吞吐不足会报错。

**这是有硬件条件的连续重放，不是对任意包络都保证零间隔。** 最关键的条件是
载波相位和 RAM 幅度可能有不同输出延迟。编译器必须证明相位改变没有碰到前一门
的任何非零输出。原来的默认 Gaussian 参数不一定满足这个条件。

## 两条管线，三个时间

AD9910 的 matched-latency 开关不能用来推断 RAM 幅度与载波相位同时到达 DAC。
数据手册给出的 RAM 幅度 matched 延迟为 106 SYSCLK；其他参数来源有自己的管线。
混合 ASF RAM 模式中的串行 POW 与 phase-autoclear 联合响应需要独立测量，不能把
single-tone 的 79 SYSCLK 或 RAM phase 的 94 SYSCLK 直接当成已验证值。
见 [数据手册的 pipeline latency 表及 RAM 操作说明](https://www.analog.com/media/en/technical-documentation/data-sheets/ad9910.pdf)
和 [ADI 对 matched-latency 范围的说明](https://ez.analog.com/data_converters/dds/f/q-a/30438/data-latency-pipeline-delay-explanation-for-ad9910-dds/212578)。

显式配置 `ram_phase_latency_mu` 开启新路径；`None` 保留旧 RF-off 路径。

```python
lab.setup.devices[name] = replace(
    lab.setup.devices[name],
    ram_latency_mu=112,  # 示例包络延迟，未测量
    ram_phase_latency_mu=80,  # 示例载波响应延迟，未测量
    ram_phase_update_duration_mu=480,  # 真实 set_pow 游标最低 416 mu（SPI coarse=8）
    ram_rearm_duration_mu=4000,
)
```

`ram_phase_latency_mu` 必须描述 POW 提交和相位累加器清零形成的**一致载波响应**。
如果这两个动作本身在输出端分两次生效，单一数值不足，此配置契约就不适用。
当前支持 `0 < ram_phase_latency_mu <= ram_latency_mu`。输出延迟可以不在 SPI 栅格上，
但每次 IO_UPDATE 必须准确落在配置栅格，不能隐式取整。

对于下一门的包络起点 B：

```text
C = B - ram_latency_mu                  IO_UPDATE 上升沿
E = C + ram_phase_latency_mu            真实载波相位响应
B                                      新包络输出起点
```

若 E 早于 B，只有原有输出在 `[E, B)` 的全部量化 ASF 样本都为零，才允许改变相位。
检查的是实际 14-bit 样本保持区间，不是解析高斯的近似值，也不是只检查最后一个零。
**相位确实可能在零幅度尾部提前改变；不宣称 DDS carrier 总是等到 B 才改变。**
保证的是已书写包络中每个非零输出样本的相位与门序列一致。

示例 `ram_boundary_sequence.py` 显式选择 `cutoff_sigma=5`、`sigma=80 ns`、
`flat_duration=0`、4 ns 采样、峰值 0.5：总长800ns、201字，48ns量化零尾
覆盖示例32ns管线差。1024门共819.2us，仍只加载这201字。
这不是编译器把默认 cutoff=4 改成 5；波形是实验作者显式选择的。
200ns缩放包络（sigma20ns）的零尾只有12ns，不能通过；即使忽略管线差，
逐门SPI准备也无法容纳。历史8us回归例中的cutoff4/20ns采样零尾为20ns，保留用于回归。

即使前后逻辑相位相同，也不能仅凭 POW 相等省略检查：phase-autoclear 会重置累加器。
只有以下 32-bit 连续性条件严格成立，才可以不依赖零输出区间：

```text
((E_new - E_old) * cycles_per_mu * FTW
 + (POW_old - POW_new) * 65536) mod 2**32 == 0
```

新路径以真实载波响应 E 做 shot-reference tracking 补偿，POW 仍有 16-bit 量化精度。
旧路径以 launch 做补偿，两者不应混为同一经过实测的相位参考。

## frame 命令和限制

- 紧接前一门、与下一门同刻的 `shift_phase/reset_frame` 合并进下一次硬件提交，
  只叠加一次 frame；命令仍受上述非零输出保护检查。
- 最后一门后的命令、或 RF-off 间隔中的命令，会产生独立硬件提交，即使没有后续
  `play`。此时载波响应准确安排在命令时刻，RF 不打开。它同时重启内部 RAM，
  因而与下一门启动太近时必须拒绝，不能让迟到的内部重启截断下一门。
- RAM 门内部的 frame 命令明确拒绝。本路径实现门边界更新，未实现门内相位调制。
- 同刻多个命令取最终 frame；不同并行分支同时写同一 frame 仍拒绝。
- profile/包络或 FTW 变化仍走完整 RF-off rearm，必须有作者显式写出的足够间隔。
  当前不支持任意包络切换也零间隔，不会暗中插入这个间隔。
- 每个活动 RAM DDS 独占其 IO_UPDATE 组；同组多个 DDS、同 DDS/shot 混用 Square/RAM
  仍不支持。独立板卡的数字事件对齐不证明时钟或光学相位已经同步。
- 表驱动导出复用驻留波形，事件表仍随门数增长；没有新增 DMA、无限循环或跨 shot 连续播放。

## 可检查的物理计划和验证

`dds_ram_phase` 是缓冲准备，`dds_ram_start` 是不阻塞的提交上升沿，
`dds_ram_commit_off` 在 8 mu 后拉低，`dds_ram_effect` 标记包络响应。
记录同时保留 `carrier_effect_mu`，不把两条管线画成同一时刻。
时间图在包络响应处分段，提前准备不会被画成输出改变。

独立 mock 检查已写入/已提交的 POW、真实 SPI 最低耗时、上升沿/下降沿配对、
实际波形数据、RF gate、输出响应和非零样本的相位保护。真实驱动审计执行安装的
ARTIQ 驱动 Python 方法体并截获 RTIO；真实编译通过 `Core.compile(host=None)` 进行。
这三类检查均不测量 DAC/RF/IQ，不验证 core FIFO、CPU slack 或真实板卡相位锁定。

```powershell
uv run --locked python examples/ram_boundary_sequence.py
.\scripts\artiq-python.ps1 scripts/verify_ram_driver.py output/ram_boundary_sequence/repeat --output output/ram_boundary_sequence/real_driver_trace.json
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_boundary_sequence/repeat --target rv32g --output output/artiq_compile/ram_boundary_repeat
```

接入实机时需要在实际 ASF RAM/CFR 配置下分别测量载波联合响应与包络响应，并检查
每块板的参考时钟、SYNC_CLK/IO_UPDATE 相位和输出路径标定。示例 112/80 mu 始终
标为未测量假设；软件通过不能改成“硬件已验收”。
