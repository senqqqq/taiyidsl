# 800 ns / 200 ns 门：相位切换、吞吐与 virtual Z

> Backend 9 更新：新 continuous 路径已实现硬件 Rz，不允许零尾提前换相。
> 本页以下旧行为属于历史分析；当前契约见 [连续 NCO](continuous_nco.md)，
> 指令分类与数值见 [ARTIQ 时间表](artiq_instruction_timing.md)。

更新：2026-11-06。当前后端不能保证任意800/200ns门序列的无间隔相位切换。
上一次8us门示例只验证了一个较宽松的声明模型，不能代表短门能力。问题包含三层：
DDS本身的数据路径、Urukul/ARTIQ控制接口的速度，以及编译器所选的写寄存器/排程策略。
“某些波形不支持”不等于“量子门或DDS从根本上不能无缝切相位”。

## 不要混淆三个时间

| 时间 | 当前数值与性质 | 对门间隔的意义 |
|---|---|---|
| 准备下一组数据 | RAM `set_pow`真实驱动游标416ns；Square完整profile为1248ns | 可以预写，但连续门的更新吞吐仍受它限制 |
| 提交时刻 | 已排程的IO_UPDATE边沿；当前高电平8mu，下降后留一个coarse低周期 | 不是调用Python后再等416ns才决定相位何时改变 |
| 提交到输出的管线延迟 | 示例carrier80ns、RAM envelope112ns；均未测量 | 固定且经过校准可提前补偿；两条管线不同还须保证输出对齐 |

416ns是安装的ARTIQ9.9664驱动`set_pow`推进RTIO游标的时间，不是相位过渡需要416ns，
也不是CPU实测执行时间。当前SPI coarse8ns、divider2，即62.5MHz；写一个POW包含
8bit指令和16bit数据。数据手册SCLK上限70Mbps，因此这个24bit串行写路径仅在线路上
就至少约343ns，不能持续每200ns接收一个新的任意相位。更早预写不能把单个缓冲寄存器
变成任意深度的队列。DMA降低CPU提交开销，也不会增加串行链路带宽。
见 [AD9910数据手册](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9910.pdf)
及 [ARTIQ AD9910驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html)。

当前编译器用480ns/1280ns准备预算，并避开不能穿越的输出边沿；不能把416ns或480ns
直接宣称为当前编译器接受的最短门周期。输出延迟、提交高低时间和全局排程也参与检查。

## 上次的零尾限制究竟是什么

当前ASF RAM路径把两个动作绑在同一个IO_UPDATE上：提交新的POW，同时从头重放RAM。
若下一门包络应在B开始，则提交C=B-La；载波相位在E=C+Lp响应。示例La=112、Lp=80：

```text
目标边界 B=800 ns
提交       C=688 ns
相位响应   E=768 ns
新包络     B=800 ns
```

如果旧包络在768..800ns仍非零，旧门最后32ns就会带上新相位。把提交推迟32ns只会令
新包络晚32ns，不能同时满足两个目标。上次实现用“原有量化零幅度尾部必须覆盖这段差”
拒绝这种错误输出；它是当前耦合路径的有条件方案，不是所有DDS实现都必须有零尾。
matched-latency不能普遍对齐RAM来源的参数，见 [ADI工程师说明](https://ez.analog.com/data_converters/dds/f/q-a/30438/data-latency-pipeline-delay-explanation-for-ad9910-dds/212578)。

与长度的关系分两种：

- 只增加flat_duration：SPI准备时间更宽裕，但sigma、采样和幅值不变时零值尾部不会因此变长。
- 整个envelope缩放：sigma和边沿也缩短，量化零尾通常随之缩短。总长度不是唯一判据。

现在示例统一用cutoff5、sigma=门长/10、flat_duration=0、4ns采样、峰值0.5：

| 包络总长 | RAM字数（含终点零） | 实际量化零尾 | 对示例32ns管线差 |
|---:|---:|---:|---|
| 800ns | 201 | 48ns | 覆盖，但还须满足准备吞吐 |
| 200ns | 51 | 12ns | 不覆盖 |

不会为了让200ns案例通过而自动增加cutoff、补零、降采样或拉长门。32ns本身是示例假设，
不是已经测量的芯片常数。更换硬件模式、时钟、同步或实际路径需要重新测量。

还有一项不能忽视的未验证假设：当前每次提交都启用phase-autoclear，并用一个Lp描述
POW更新与累加器清零的联合响应。ADI论坛有带示波器结果的用户报告称两者可能错开
1–2个SYNC_CLK，且matched-latency不能消除毛刺；这是未获厂商答复的实验报告，不能
当成已确认规格，但足以说明不能靠一个mock里的“同时生效”就证明实机无毛刺。
[原始测量报告](https://ez.analog.com/data_converters/dds/f/q-a/164335/ad9910-autoclear-phase-accumulator-glitches)。
固定载频长门序列的后续实现应优先改为shot起始建立参考后保持相位累加器运行；这能消除
逐门autoclear引入的清零瞬态和低位丢失，仍不能单独解决SPI吞吐或RAM两路延迟差。

## 当前800/200ns结果

`examples/short_gate_timing.py`逐例编译、记录拒绝理由，对成功例运行auto/unrolled
等价检查。矩阵包括40种情况：直接phase、硬件shift/reset和Rz、单双独立板、同相位
对照，以及明确不导出的Lp=La反事实诊断。

| 无间隔X/Y/X序列 | 800ns/门 | 200ns/门 | 当前主要原因 |
|---|---|---|---|
| Square，单板或双板 | 拒绝 | 拒绝 | 每门完整profile写入预算1280ns |
| ASF RAM，单板 | 声明模型下通过 | 拒绝 | 200ns零尾不足，且串行POW吞吐也不足 |
| ASF RAM，两块独立板 | 拒绝 | 拒绝 | 当前全局串行化两次480ns准备；200ns还受单路限制 |
| 无新参数写入的精确同相位重复门 | 两档均可通过模型 | 两档均可通过模型 | 不能据此推断不同相位门同样可行 |

即使在反事实模型里让Lp=La，RAM200ns仍因准备窗口失败。因此只修“相位与包络对齐”
仍不够。两块独立SPI板的800ns失败包含compiler限制：当前把驱动调用全局串行执行。
若改成明确建模各SPI总线的异步提交/忙碌时间，可以重叠不同板的传输；同一SPI上两个
416ns传输则已超过800ns，不能靠重新排程解决。

## 有哪些解决路径

| 方向 | 能解决什么 | 限制与当前状态 |
|---|---|---|
| 保持NCO相位累加器运行，编译逻辑frame | 避免逐门清零、减少不必要POW写入 | 硬件连续累加路径尚未实现；不能提高每门必写相位时的SPI速度 |
| 独立板SPI异步排程 | 改善800ns多路准备吞吐 | 当前compiler尚未实现；共享总线仍串行 |
| Square预装若干相位profile，边界只选择profile | 避免每门写64bitprofile；原生DDS profile引脚可快速切换 | 标准Urukul profile选择仍经CPLD SPI，并非现成TTL命令 |
| RAM同时存phase+amplitude，并补偿两路采样延迟 | 有可能去掉对原包络零尾的依赖 | 需要改变RAM格式/库身份、校准及验证；不能只切换RAM_DEST后宣称解决 |
| FPGA定时相位/波形指令队列、快速profile控制或I/Q波形后端 | 为任意200ns长序列提供足够的实时选择与相位带宽 | 需要相应硬件/固件能力与新的后端契约，当前未实现 |

预加载profile并非当前板卡上的万能解法。`scripts/audit_phase_paths.py`执行已安装
驱动体、截获RTIO，无硬件访问，得到：

| 操作 | Rev8 | Rev9 |
|---|---:|---:|
| set_pow | 416ns | 416ns |
| set_profile | 416ns（四通道共用选择） | 896ns（可逐通道选择） |
| 写完整DDS profile | 1248ns | 1248ns |
| 一次批量写四通道profile选择 | 原本共用 | 896ns；四次逐路调用则3584ns |

因此现有Rev9 API即使预装好了profile，也不能持续每200ns或800ns切一次。
这是持续更新速率限制，不是200ns脉冲的最短长度限制。某些只有一次切换的有限X→Y
程序可以提前预装并在RF-off预卷开始那一次选择事务，随后在事务途中发独立RTIO边沿；
当前opaque SPI排程不支持这种跨边沿提交，也需要改造，不能用它宣称任意长序列可行。
标准Urukul gateware暴露IO_UPDATE/RF switch TTL，profile由CPLD配置寄存器控制；
不能把“DDS芯片有profile引脚”直接说成“现有实验设备已有快速profile通道”。
见 [Urukul驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)
和 [ARTIQ Urukul gateware接线](https://github.com/m-labs/artiq/blob/master/artiq/gateware/eem.py)。

RAM polar模式的设计候选：在固定载频、不逐门清零、同一包络连续重复的条件下，
数据表RAM phase94与amplitude106 SYSCLK的差可用采样错位补偿。1GHz/4ns采样时差3点，
可研究把幅度循环提前3点、用前一bank尾部提供后一门前3点，phase bank保持本门轴角。
需要RF-off预卷建立第一门前缀；四轴的800ns/200ns包络分别约800/200字（不另存终点零）。
这只是根据管线模型推导的候选，尚未实现/实测；不同包络、任意角度及profile选择吞吐
还需单独解决。固定少量profile的内部循环可重放固定motif，不能替代任意长随机门序列。

## 已实现的Rz / virtual Z

API是`rz(q, angle)`，大写`Rz`为同一函数，角度rad，可扫描、repeat，逻辑时长为0。
它保留门级来源，但与 `shift_phase` 使用同一个持久 frame F。按以下约定：

```text
Rz(theta) = exp(-i theta Z/2)
R_phi(alpha) = exp[-i alpha (cos(phi) X + sin(phi) Y)/2]
shift_phase(delta): F += delta
Rz(theta): F -= theta
reset_frame/reset_phase: F = 0
后续drive phase = pulse.phase + F
```

所以 `Rz(q,+pi/2)` 对应 `shift_phase(q.frame,-pi/2)` 的驱动更新。正Rz在上述约定下令后续
drive phase减去该角；符号由矩阵恒等式验证。若需抵消已知逻辑Z误差delta，可写
`Rz(q,-delta)`。驱动phase=0/π/2分别表示X/Y轴；phase=π是负X轴，不是Z旋转。

```python
play(q.raman, envelope)  # 800ns示例包络
Rz(q, -math.pi / 2)  # 零时长；后续X驱动轴在物理上变为Y
play(q.raman, envelope)
Rz(q, math.pi / 2)  # 可以位于最后，无需凭空打开RF
```

ARTIQ 后端会把 Rz 编译为实际的相位准备/提交；Spectrum 把它写进后续样本。
实际门仍须携带正确硬件相位并通过准备/生效时序检查，
所以`play(200ns) → Rz → play(200ns)`不会因此绕过当前硬件带宽限制。初始Rz可以并入
第一门预加载；末尾Rz保留在逻辑IR及`compiled.virtual_z_frames(point)`，不要求后续play。
`compiled.frame_phases(point)` 是最终运行 frame；virtual_z_frames 仅记录最近 reset
之后的 Rz 贡献，不应再从运行 frame 扣除。

frame作用于qubit的raman/rf驱动，不改变TTL或普通光学输出；reset_frame
清整个 F，包括 Rz 的贡献，但不重启载波。reset_phase 另清继承调制并重建载波参考。
当前要求Rz与同qubit的门在同一serial分支中书写，拒绝门内或有歧义的跨并行分支更新。
各qubit可在自己的并行分支中独立使用。

virtual Z是以未来控制/测量参考的改变实现逻辑旋转，不是在RF关闭时让裸原子状态凭空
产生一个即时物理相位跃变。Z基人口读出不受末尾V影响；横向层析/其他相位敏感操作
必须保留并使用这个参考。当前尚无通用多比特门框架，不能假设任意外部控制都已自动
消费frame。原理参考 [McKay等，Efficient Z gates for quantum computing](https://link.aps.org/doi/10.1103/PhysRevA.96.022330)。

## 复现与验收边界

```powershell
uv run --locked python examples/short_gate_timing.py
uv run --locked python examples/ram_boundary_sequence.py
.\scripts\artiq-python.ps1 scripts/audit_phase_paths.py
```

`output/short_gate_timing/summary.json`保存接受与拒绝理由，`driver_phase_paths.json`
保存驱动版本、源码哈希与游标记录。代码单元测试、mock、真实驱动离线审计和真实
ARTIQ编译分别记录；它们都没有测量实际POW/autoclear瞬态、模拟幅相或板间时钟。
当前仍需要在准确板卡配置下测量响应，再确定800ns可部署路径；通用200ns实时换轴
应先确定具备足够带宽的控制接口，不能继续靠长脉冲示例推断。
