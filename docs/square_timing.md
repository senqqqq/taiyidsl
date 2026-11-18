# Square 连续更新与硬件相位操作

> Backend 9 的新版参考配置使用 continuous NCO，仅在 shot 前建立一次相位参考。
> 以下逐门 autoclear/共享组全量重写的描述保留为 legacy_tracking 历史契约；
> 当前行为见 [连续 NCO](continuous_nco.md)。

ARTIQ 后端的 timed Square 路径把寄存器准备、IO_UPDATE 提交和输出生效分开。
用户指定的 `play`、`delay` 和相位命令时间不因驱动配置而移动；无法满足原时序时
编译失败，不自动插入空白。这里的 gapless 指没有增加用户未指定的输出间隙，
不是保证任意短门都能超过硬件更新带宽。

## 设备契约

在实验室的 `LabConfig` 中显式声明：

```python
Device(
    "dds0",
    "dds",
    bus="spi0",
    update_group="board0",
    square_latency_mu=80,
    square_prepare_duration_mu=1280,
    spi_ref_period_mu=8,
)
```

以上是 1 ns/mu、1 GHz DDS 下的示例假设，80 ns 不是实测标定值。
`reference_lab` 使用同一示例契约。真实设备应按其时钟、IO_UPDATE 对齐、DDS
matched-latency 数据路径及输出测量填写；光路和 RF 开关延迟仍由原有设备及
`DeviceCalibration.latency_mu` 描述。数字事件对齐不证明原子处光场相位已校准。

`square_latency_mu=None` 保留旧的 RF-off `set_mu(TRACKING)` 路径。
该旧路径的 `config_duration_mu` 默认已修正为 3000 mu，不能再使用原来错误的
1000 mu 假设。安装的 ARTIQ 9 驱动在 SPI coarse period 为 8 mu、零 update delay
时，完整 tracking `set_mu` 推进游标 2664 mu；新路径只写 profile 的 `write64`
推进 1248 mu，示例为其预留 1280 mu。

编译器及导出程序检查驱动最低预算；导出程序还校验真实 SPI 时钟、core/DDS 时钟、
直接 TTL IO_UPDATE、共享 update 组接线和所支持的 profile API。
当前 timed 路径要求 `io_update_delay=0`，不自动使用非零驱动校准值。

## 准备、提交与生效

每个 shot 在定时原点之前配置 single-tone profile 7、matched latency 和
phase-autoclear，RF 保持关闭。定时区间内：

1. `dds_prepare` 用 `write64` 写 profile 缓冲，允许前一门仍在输出；该动作不提交。
2. `dds_commit` 发出 IO_UPDATE 上升沿；`dds_commit_off` 在 8 mu 后发出下降沿。
   两个调用均不把 8 mu 高电平当作 CPU 独占窗口，可支持多路同刻提交。
3. `dds_effect` 表示声明的管线延迟后发生的输出状态变化，不额外发送 RTIO 指令。

对于逻辑输出时刻 `T`：

```text
effect = T - device.latency_mu - calibration.latency_mu
commit = effect - square_latency_mu
prepare 必须在 commit 前完成
```

如果只是 RF-off 期间为下一次开关打开准备状态，寄存器可以在打开之前提前生效，
从而兼容更细的 RF 开关栅格；RF-on 的逻辑时刻不变。连续输出中的参数边界及
显式相位命令则要求精确的 effect/commit 栅格，不能用提前提交改变这些时刻。

相邻 Square 的 RF 门控区间合并，即使频率、相位或幅度不同也不在边界重复关开
RF 开关。准备动作可以放在前一门内部，但不得越过前一次提交、侵犯显式 reservation，
或跨过不兼容的 RTIO 提交边沿。准备暂时全局串行；没有无限缓冲或任意带宽承诺。

同一 `update_group` 的多个参与 DDS 共用一个提交边沿。由于 persistent
phase-autoclear 会在该边沿清除各通道的累加器，每次组提交都会重新准备所有参与
DDS 的状态，包括参数未变的通道，并重新计算 tracking POW。不同组可同时提交。
保持相位的精度受 16 位 POW 量化限制，不宣称累加器低位在重新清零后完全保留。
同一活动 update 组暂不混合 timed Square、旧 `set_mu` 路径或 RAM 路径。

## 相位命令是真实硬件更新

`shift_phase(q.frame, angle)` 和 `reset_frame(q.frame)` 保持零逻辑时长，但在 timed
Square 后端会安排真实 profile 写入和定时提交。它们不再仅用于修改后续 `play`
的编译期参数。

- `shift_phase` 改变 frame 偏移，并在写下的逻辑时刻改变相关 DDS 的输出相位。
- `reset_frame` 清除 frame 偏移，恢复当前脉冲自身的相位及硬件校准偏移；不是把
  RF 载波在每个时刻都强行设为绝对相位零。
- 命令可以在 RF 关闭的间隔中更新保持状态，也可以通过并行命令分支放在长
  Square 内部。后面没有新的 `play`，也不会因此删除该硬件更新。
- 命令作用于该 qubit 在本 shot 使用的 DDS；若完全没有 DDS `play`，从已映射
  的 `raman/rf` 默认参数建立状态并更新，RF 始终关闭。不会凭空打开输出。
- 同一时间点的连续相位命令合并为该边界的最终状态；同时开始的 `play` 只叠加
  一次 frame。不同并行分支同时写同一个 frame 会被拒绝。
- 相位仍采用共同 shot 参考下的 tracking 语义。任意跳频并不自动保持历史频率
  积分意义上的连续相位。

RAM 门边界硬件 frame 与有条件的连续重放已在 backend 7 实现，需要另外配置
`ram_phase_latency_mu`；详见 [RAM 边界时序契约](ram_boundary_timing.md)。旧 RAM
路径仍拒绝硬件 frame。缺少映射或可确定 DDS 状态时，不能静默降级为软件操作。

可运行例子 [square_timing.py](../examples/square_timing.py) 展示：两块独立板卡的
连续幅频相更新，以及 12 µs 长 Square 内在 4/8 µs 执行 shift/reset 的命令分支。
运行示例只生成离线导出和时间图，不连接仪器。

## 验证与范围

mock 分开保存缓冲状态和提交状态，按实际驱动 SPI 序列公式推进准备游标；检查
缺失写入、提前提交、共享组漏准备、缺失下降沿、错误脉宽及输出生效时刻。
物理时间图在 `dds_effect` 划分连续输出片段，不能把 `dds_prepare` 画成输出变化。

`scripts/verify_square_driver.py` 使用安装的真实 ARTIQ Python 驱动函数体和虚拟
RTIO，核对生成程序的 SPI 时长、寄存器字及数字边沿。它不实例化真实通信设备、
不运行硬件 kernel。真实 ARTIQ 离线编译是另一项检查，二者都不能代替 RF/IQ
输出测量、时钟同步校准、RTIO analyzer 和 core log 验收。

`DeviceSetup.rtio_coarse_mu` 和 `rtio_lane_count` 声明目标的 coarse 时间单位及
SED lane 容量；默认均为 8。编译器检查同 coarse 周期的提交事件数量，导出原点
对齐该 coarse 栅格，并校验实际 core 的 `ref_multiplier`。lane 数量需与 gateware
配置一致，CPU 提交吞吐和 FIFO 余量仍需实机验收，编译检查不等于不会 underflow。

timed Square 当前逐点生成 kernel，`auto` 与 `unrolled` 均使用同一逐点路径。
长序列仍是有限的静态事件序列；没有新增 DMA 或硬件循环。shot 之间仍经过 host，
不保证跨 shot 无缝衔接。RAM 连续重放由独立时序契约覆盖；任意门内幅相调制尚未实现。
