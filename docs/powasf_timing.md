# 可选 POWASF：不改包络的幅相对齐与硬件 Rz

本路径通过 `Device(ram_destination="powasf", nco_mode="continuous", ...)`
显式启用。默认仍是 ASF RAM；原生 Square 仍使用 single-tone。
POWASF 的离线通过只表示**在显式延迟契约下，打包样点、输出边界和驱动调用一致**。
它不代表延迟已经实测，不代表板卡输出、长期 RTIO 吞吐或原子处相位已验收。

本页的 Rev8/Rev9 是 **Urukul CPLD 协议版本** `proto_rev=0x08/0x09`，
不是 ARTIQ 软件主版本，也不是 PCB 的 v1.x 版本。实际装机硬件和 CPLD 固件决定
可用协议，`device_db.py` 和 DSL 配置必须与其匹配，不能为了让排程通过而改填 Rev8。
兼容硬件有时可通过另行刷写固件改变协议，但本项目不自动执行这类操作。
[Urukul 官方硬件/固件兼容表](https://github.com/sinara-hw/Urukul/wiki/Firmware)
分别列出 PCB、固件版本和协议编号；当前项目没有提供实际仪器的 device_db，
示例中的版本不能作为实验室实际型号的证据。

## 同一个 RAM 字为什么仍需要补偿

AD9910 的 POWASF 字把 16 位 POW 放在 `[31:16]`，14 位 ASF 放在
`[15:2]`。预加载这些字的 SPI 时间与播放时两条数字管线的延迟是两件事。
同一字被读出后，相位和幅度进入不同管线；共同读取不保证共同到达输出。
数据手册 RAM 数据路径的 94/106 个 SYSCLK 数字属于播放数据到 DAC 的路径，
不是单独写入相位/幅度寄存器的 SPI 耗时，也不能直接当作整条板卡链路的标定。
[AD9910 数据手册](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9910.pdf)
及 [ADI 对 matched latency 的说明](https://ez.analog.com/data_converters/dds/f/q-a/30438/data-latency-pipeline-delay-explanation-for-ad9910-dds/212578)
说明 RAM 路径不能套用 single-tone 的幅相匹配结论。

本实现保持每一个 ASF 样点、采样间隔、门起止时间不变，仅调整 POW 样点。
设包络延迟为 `L_env`、RAM 相位延迟为 `L_phase`，且
`L_env - L_phase = m × sample_interval >= 0`。每个门的相位数组前 `m`
个点保持进入该门前的相位，其余点使用该门的目标相位。

例如，示例**假定而未测量** `L_env=112 ns`、`L_phase=100 ns`，采样间隔
4 ns，故 `m=3`。要在 T 开始 Y 门，底层 profile 在 `T−112 ns` 启动：

| RAM 点 | 相位到输出时间 | POW | 幅度到输出时间 | ASF |
|---|---:|---|---:|---|
| 0 | T−12 ns | 前门相位 | T | 原包络点 0 |
| 1 | T−8 ns | 前门相位 | T+4 ns | 原包络点 1 |
| 2 | T−4 ns | 前门相位 | T+8 ns | 原包络点 2 |
| 3 | T | Y 相位 | T+12 ns | 原包络点 3 |

因此相位在 T 改变，ASF 在 T 开始原包络。这里没有借用前门零尾提前换相，
没有删除或补齐 ASF 样点，没有插入门间空白，也没有把整个序列拼接成一个波形。
不满足整数样点差、目标门短到无法容纳补偿，或切换会截断必要的相位样点时，编译拒绝。

## 两类触发与配置

| 字段 | 定义 |
|---|---|
| `ram_latency_mu` | IO_UPDATE 上升沿至 RAM 包络输出 |
| `ram_polar_phase_latency_mu` | IO_UPDATE 上升沿至 RAM 相位输出 |
| `ram_profile_latency_mu` | 真实 `cpld.set_profile` **结束游标**至 RAM 包络输出 |
| `ram_profile_phase_latency_mu` | 同一选择调用结束游标至 RAM 相位输出 |
| `ram_profile_control` | 实际使用的 `urukul_rev8` 或 `urukul_rev9` 驱动协议 |
| `nco_epoch_latency_mu` | shot 预备阶段解除 NCO clear 至连续相位参考建立 |

这些字段必须显式声明；不能从旧 autoclear 延迟或独立 POW 寄存器延迟猜出。
profile 选择的结束游标是**软件契约的锚点**，并不声称物理选择引脚刚好在该时刻改变；
实际 SPI 完成、引脚响应、DDS 同步及输出管线全部需要计入所声明并标定的延迟。

```python
Device(
    "dds0",
    "dds",
    bus="spi0",
    update_group="board0",
    nco_mode="continuous",
    nco_epoch_latency_mu=80,
    ram_destination="powasf",
    ram_latency_mu=112,
    ram_polar_phase_latency_mu=100,
    ram_profile_control="urukul_rev8",
    ram_profile_latency_mu=112,
    ram_profile_phase_latency_mu=100,
)
```

上面全部延迟是示例输入。不要原样当成实验室校准参数。1 mu=1 ns、SYSCLK=1 GHz
及 SPI coarse=8 ns 也都是示例条件。

相同 profile 连续调用使用 IO_UPDATE 重触发；不同 profile 通过真实 CPLD SPI
选择触发。选择后不附加第二次 IO_UPDATE，以免再次重启 RAM。
RAM 字与 profile 描述符都在 shot 之前、RF-off 时装载。
当前导出支持同 shot 的跨 DDS POWASF、timed Square、continuous ASF RAM 与 TTL
组合；详见 [混合播放路径](mixed_pulse_paths.md)。这解决了前一轮专用 exporter 的
总禁令，所有活动 DDS 仍需连续 NCO 和明确的时序契约，旧 opaque 配置路径不混入。
同一 DDS 的模式混用、活动通道共享 IO_UPDATE，以及选择 SPI 传输期间插入同路
IO_UPDATE 也保守拒绝。这些是当前实现范围，不是对所有硬件方案的否定。
每个 shot 只建立一次 NCO epoch，随后固定 FTW、clear/autoclear 均为零。
载波的完整 32 位相位累加保留，epoch 的 16 位 POW 修正误差是固定的，
不会在每个门边界重新累积或清零。

## 幅相对齐与 profile 选择速度是不同约束

| 情况 | 离线契约下的结论 |
|---|---|
| Rev8，800 ns 连续 X/Y，112/100 ns、4 ns 采样 | 相位补偿与 416 ns 选择调用可排程；仍须实测 |
| Rev9，800 ns 连续 X/Y | 每次真实选择调用 896 ns，持续换 profile 的控制带宽不足，拒绝 |
| Rev8/Rev9，200 ns 单门 | 可在门前完成选择；门长不必大于准备调用时间 |
| Rev8，200 ns 持续换 profile | 416 ns 控制调用带宽不足，拒绝 |
| 两个独立 SPI，800 ns 同步 X/Y | 允许准备调用重叠；各自按延迟补偿输出边界 |
| Rev8 同一个 CPLD 上多个独立相位通道 | profile 引脚共享；本轮保守拒绝，没有声称每路可独立选择 |

“200 ns 单门通过”明确指每个相关通道的一次短门调用，不包含 200 ns 连续 X/Y。
跨 shot 重复一个短门时，每个 shot 可重新预备；shot 内连续调用则必须另外核对控制吞吐。
保持**同一驻留 profile、相同硬件相位状态**时可以仅用 IO_UPDATE 重触发，例如从初始
X 状态连续 X/X/X；换 profile 或换轴仍受 416/896 ns 选择成本限制。
即使逻辑上连续 Y/Y，首门的 X→Y 相位前缀与后续 Y→Y 前缀也可能不同，
因此不能把“同一 profile 重触发”扩大成“所有同轴门都能每 200 ns 重复”。

416/896 ns 是 `spi_ref_period_mu=8` 时的真实驱动**游标推进**，不是幅相响应延迟。
它们分别为 `52C` 和 `112C`。离线排程满足调用占用并不证明 CPU、RTIO FIFO
能无限期提交这些事件；本轮也没有支持独立 RTIO core 的同步启动。
[ARTIQ Urukul 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)

本轮不启用 DDS 内部自动 profile 序列引擎。800 ns 包络保留末端零样点时是
201×4 ns 的存储块；直接按存储点数自动链到下一个块会得到 804 ns，而不是
用户要求的 800 ns。不能删除末端样点或偷偷改变门长来绕过这件事。

## 独立 Rz 与小波形库

`RAM_DEST_POWASF` 下输出相位来源于 RAM POW 样点，独立 POW 寄存器不是可叠加的
全局相位旋钮。因此硬件 Rz 通过选择驻留的**零 ASF 相位保持块**实现，
不通过 `set_pow()` 冒充相位更新。该块有 `m+1` 个字：前 `m` 点保持旧相位，
最后一点为新相位，全部 ASF 为零。它在指定 T 改变实际载波相位偏移，并保持到
下一次明确更新；RF 开关保持关闭，没有额外的实体 Z 驱动脉冲。

- 正 `Rz(theta)` 使统一驱动 frame F 减 theta，与 shift_phase(frame, -theta) 共用更新机制。
- `shift_phase` 更新 F，`reset_frame` 清整个 F；后续驱动参考是 base+F。
- `reset_phase` 另重建载波参考，编译成驻留 POWASF 样本的相位补偿，不通过独立 POW 寄存器冒充。
- 独立、末尾、没有任何 play 的 Rz 均有真实 profile 选择/重触发和相位效果。
- 同一边界的 Rz 与下一门可合并为一次选择，但相位仍必须在该边界生效。
- RF-off 时也比较相位，不能因为没有可见输出就把指定 T 的硬件更新延后。

库按实际 packed words、采样步长和模式去重。相同包络、相同进入/目标相位的门可
重复任意多次，不会按调用次数复制波形。由于前缀保留进入相位，`X→Y` 的 Y
变体与 `Y→Y` 的 Y 变体可能不同；“小库复用”不等于任意相位角只占一个 profile。
当前限制仍是每 DDS 1024 字、8 个 profile，初始 RF-off 保持块也占一个 profile。
典型长 X/Y 序列占初始 1 字加 3×201 字，即 604 字，而不是 1024 门×201 字。
任意很多不同角度的 Rz 或很多相位转换会耗尽库，必须明确报错。
波形库占用不随调用次数增长，但 core 中的事件表及导出的编译计划仍随门数增长；
当前编译计划 JSON 还会重复保存 bank 元数据，不能据此宣称编译内存为常数。

## 可重复的离线验证

```powershell
uv run --locked pytest tests/test_ram_polar_validation.py --basetemp=.test-tmp-polar
uv run --locked python examples/powasf_sequence.py
./scripts/artiq-python.ps1 scripts/verify_polar_driver.py output/powasf/rev8_800_xyyx
./scripts/artiq-python.ps1 examples/artiq_offline_compile.py output/powasf/rev8_800_xyyx --target rv32g --output output/powasf/rev8_800_xyyx/real_compile
```

示例生成 `output/powasf/summary.json` 和各案例的 ARTIQ 源码/编译计划。
包含 800 ns 长 X/Y、X/Y/Y/X、小库重用、独立双 SPI、200 ns 单门与并行配合、
独立/末尾/同刻合并 Rz，以及 Rev9 800 ns 和 Rev8 200 ns 带宽不足的拒绝案例。
用户显式写出的 idle 与编译器插 gap 不同；独立 frame 示例中的 800 ns idle
属于实验定义，编译器不添加 idle。

`taiyidsl.polar_validation.verify_polar_output` 从**实际 packed RAM 字**解码 POW/ASF，
按选择/IO_UPDATE 事件及各自延迟重放两条输出管线，和原始逻辑 pulse/phase 命令比较。
它不以 `dds_polar_effect` 声称的目标值或 bank 的 `asf/pow_samples` 注释为真值。
比较覆盖门内全部量化包络、每个相位边界、RF-off 期间、末尾相位保持及固定 NCO epoch。
因此删除补偿前缀、改坏 ASF、漏掉末尾 Rz、使用错误符号或逐门改 FTW都会失败。

应分别记录以下验收层级，不能相互替代：

| 层级 | 验证内容 | 不证明 |
|---|---|---|
| 独立输出模型 | packed words、声明延迟下的严格幅相边界、RF 窗口 | 声明延迟正确 |
| 导出/mock 等价 | 生成代码与计划、不同导出方式一致 | 真实驱动内部实现 |
| 真实驱动虚拟 RTIO 审计 | 实际 SPI 调用长度、寄存器/RAM 数据、选择及数字边沿 | CPU 实时吞吐、模拟输出 |
| 真实 ARTIQ 离线编译 | kernel 能被指定版本真实编译器接受 | 板卡正确同步/物理 gapless |
| 实机输出验收 | 时钟、DDS 同步、选择接口、幅相延迟及长序列稳定性 | 本轮未执行 |

严格的 deterministic/synchronized/gapless 结论目前只对明确列出的**离线契约**成立。
共同参考时钟、DDS 同步和模拟链路校准仍是实机验收前提。

2026-11-09 本轮实际检查：11 个验收案例以及额外的 800 ns 展开导出案例，
共 12 份导出通过 **ARTIQ 9.9664 真实驱动体的虚拟 RTIO 审计**；
同 12 份导出的初始化和 shot kernel 共 **24 个 kernel 通过 rv32g 真实离线编译**。
其中 Rev9 同轴 800 ns 序列只在首门选择 profile，后续通过 IO_UPDATE 重触发，
与 Rev9 持续换 profile 的拒绝案例分开验证。
驱动审计逐项比较实际 SPI RAM 数据顺序、profile 描述符、选择调用时长、
CPLD 选择位、IO_UPDATE/RF 边沿、连续 NCO clear/release 和 SED 提交顺序。
审计报告保留 ARTIQ 版本、驱动文件 SHA256 与导出源码 SHA256。
这些检查没有执行任何硬件或真实 kernel，也没有测量幅相输出延迟。
最终验收和哈希汇总保存在 `output/powasf/verification_summary.json`。
