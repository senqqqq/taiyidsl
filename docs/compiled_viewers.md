# 编译结果查看器与混合实验示例

项目不 fork。原来的纯 ARTIQ 示例、后端和优化保留；新增实验统一放在
`examples/artiq_spectrum/`，输出在 `output/artiq_spectrum/<实验名>/`。
配置仍显式选择后端，不通过 `trigplay` 自动切换。

## 两个不同尺度的查看器

`compiled.visualize(path)` 是宏观 pulse sheet，纯 ARTIQ 和混合后端都可用。
默认把所有扫描点和 shots 按实际执行顺序排在同一横轴，同一信号/资源使用固定泳道，
不再为每个 shot 新建一组泳道。矩形宽度表示持续时间，frame 更新为零时长标记。
逻辑视图显示 DSL 事件，物理视图可检查 Spectrum 卡/通道及驱动事件。

鼠标悬停只显示简短的信号/操作、point/shot、起止时间、时长和关键参数。
完整参数、来源路径和逻辑关联放在点击后的详情区，不在 hover 中展开长 JSON。
多次执行中的波形详情链接保留原来的 point、shot 和局部时间，显示偏移不会变成波形相位。

默认先看完整内容，再按需筛选。顶部只保留常用操作；单位、点/shot 筛选、驱动细节、
精确窗口等收进主图上方的 Advanced；两个查看器采用相同位置。拖动缩放、恢复视野均可用；大量 executions 按窗口分页，
默认最多50次，并明确显示数量，不为海量 shots 一次分配所有图形对象。

`compiled.waveform_simulator(path)` 是 Spectrum AWG/DDS 共用的输出查看器；
`compiled.export_report(...)` 自动生成相同的 `waveform.html`。
默认将多个 point/shot 的完整 Spectrum 窗口按执行顺序放到一条横轴，并在同一张图上显示
**全部卡的全部已启用通道**。每条物理输出使用固定颜色，点击图例可显示/隐藏该输出
在所有 executions 中的波形；缩放后保留图例显隐状态。卡/通道选择不再是查看主图的前提，
高级选项仅用于定位某个脉冲或精确时间窗口。

两种累计时间轴都属于编译结果的展示投影，不是实测墙钟时间。Pulse sheet 保留已知
控制窗口、物理 pre-roll、固定 native 时段和 DAC 播放尾段；waveform 默认聚焦完整
Spectrum 窗口，保留组内空闲、不同卡的补偿和物理尾段。主机等待、上传和可变 native 准备
时长没有编译期确定值，执行边界明确提示这些时间未包含。不能据图形拼接断言两次
真实触发之间没有间隔。每次执行仍使用原始局部样本和 phase，不沿展示横轴重算载波。

全序列概览从最终量化 int16 segment bytes 和静态 step table 回放，支持跨 segment
边界和长零段循环，不压掉门内等待。长窗口显示标注的 min/max：每个连续时间 bin
保存真实 DAC 最小/最大值，不抽取稀疏点冒充 RF。放大后在预算内显示逐样本值。
与多次执行相交的解码工作按窗口限制；重复 shot 复用已有 packed 样本。

HTML 按内容 hash 去重、压缩非零 segment，零段保持符号表示。文件大小随不同驻留
样本总量增长，不随长 delay 或 shot 数量线性增长。默认在物化前限制唯一非零 recipe
段的原始数据总量为64 MiB；大量扫描点可拆报告，或显式调整 max_embedded_bytes。
解压缓存最多16 MiB。两个 HTML 都不需要仪器、Spectrum SDK 或服务器。

交互功能的 Node/DOM 单元检查与真实浏览器验收分开记录。当前交付的本地 HTML
可手动用 Edge/Chrome 打开；自动化工具对 file:// 的限制不能通过改 Edge 设置解除。

包含原生DDS卡时，查看器自动添加频率（MHz）、幅度（FS）两行，与AWG共用横轴和缩放。
每个tone按卡、通道和core标识，图例可单独显隐。DDS由实际量化命令的分段线性斜率生成，
保留频率pin时的更新延迟和端点修正；幅度曲线不随AWG的DAC码/电压单位切换。
已知控制窗口内、多个trigger region之间的hold保持非零，未知shot/host间隔仍不赋时长。
只有DDS的实验也可显示；没有DDS的旧AWG报告保留原单行显示。
`CompilationStore.export_report(...)` 的按chunk加载查看器也使用相同DDS数据与显示规则，
仅加载与窗口相交的AWG样本分片。

**AWG显示最终DAC样本，Spectrum原生DDS显示命令预测的频率与幅度。**
纯 ARTIQ 的 DDS/RAM 操作继续在 pulse sheet 中检查；本期没有新增 Urukul 模拟链路
模拟器。波形也不模拟 AOM、放大器、滤波器、线缆、光学传递或量子态演化。

交互参考官方 [LabOne Q Pulse Sheet Viewer](https://docs.zhinst.com/labone_q_user_manual/core/functionality_and_concepts/03_sections_pulses/concepts/02_pulse_sheet_viewer.html)
的宏观检查，以及 [Output Simulator](https://docs.zhinst.com/labone_q_user_manual/core/functionality_and_concepts/10_advanced_topics/tutorials/00_output_simulator.html)
的短窗口输出检查。实现和数据源属于 TaiyiDSL 自己的编译产物。

## Python 用法

```python
compiled = lab.compile(exp)  # lab.backend == "artiq_spectrum"
bundle = compiled.export_report("output/my_experiment", source_path=__file__)
print(bundle["index"])

compiled.visualize(
    "output/my_experiment/pulse_sheet.html", waveform_path="waveform.html"
)
compiled.waveform_simulator("output/my_experiment/waveform.html")

# 相对逻辑 shot T0 的 2 us 窗口，不接触仪器。
window = compiled.simulate_output(point=0, start_s=20e-6, duration_s=2e-6)
```

`simulate_output` 返回窗口元数据和每卡/通道 traces，包含时间、样本编号、DAC
整数码、归一化值和按声明量程计算的理想电压。默认 `max_samples=100000` 按每通道
限制，过大请求报错。窗口选择真实采样网格上的点，不将其伪装成任意请求时间。
重复 shot 回放同一编译点，图不假定 host 在 shot 之间的间隔固定。
该Python接口仍专用于精确AWG样本，DDS曲线不混入其DAC traces。DDS显示直接由通用viewer处理，
不需要用户为move实验另写绘图代码。

## 五个独立实验

运行 `uv run --locked python examples/artiq_spectrum/<文件名>.py`。
也可运行 `uv run --locked python scripts/export_hybrid_examples.py` 一次生成全部
示例及已有的并行 clock/Rydberg 演示，总入口为 `output/artiq_spectrum/index.html`。
另有 `staged_workflow.py` 展示把多个操作封装为准备/冷却/读出 DSL 函数。
这不是已有原生 `@kernel` 的接入器，见 [原生 ARTIQ 接入边界与方案](native_artiq_integration.md)。

| 文件名 | 扫描与逻辑结构 | virtual Z / frame |
| --- | --- | --- |
| `rabi` | 6 幅度 × 3 门宽，单核自旋门 | H=V=0 |
| `ramsey_long_delay` | X90 → 10 us / 1 ms / 100 ms delay → 第二门 | Rz(+90°) 让第二门 base-X 变成 −Y；末尾 V=+90° |
| `spin_echo` | X90 → τ → Y180 → τ → X90 | 用 V 改中间门轴，再恢复 V |
| `spin_locking` | +Y90 → +X lock → −Y90 | reset_frame 清整个 F，默认末尾恢复相位参考 |
| `allxy` | 标准 21 对门，I 用 200 us 等待槽 | 用 V 选择 Y/y 并恢复；I/I 无硬件工作 |

默认 30 kHz、200 us 门槽，以 amplitude 0.4 / 0.2 分别示意 π / π/2，未经标定。
cooling/readout 也是占位程序。AllXY 顺序核对
[Reed 学位论文 Table 5.1](https://rsl.yale.edu/sites/default/files/2024-08/2013-RSL-Thesis-Matthew-Reed.pdf)。
理想旋转矩阵检验验证轴和符号，不产生或冒充实际测量数据。

这些单比特实验用 A 卡 CH0；同步组激活时 B 卡两路输出零，A 卡 CH1 关闭。
`examples/spectrum_hybrid.py` 仍包含 clock/Rydberg 并行及局部相位调制示例。
AllXY 的 I/I 保留 400 us 逻辑等待，但跳过 Spectrum 上传/触发。

每个 `index.html` 含完整 DSL、所有扫描点的实际 segment/step 表、触发表、末尾 frame
和 waveform 链接。`point_N/group_N/card_N/` 保存 packed binary 和 CSV；目录用编号，
卡名在表和 JSON 中说明。`export/artiq/experiment.py` 保留原生 ARTIQ 导出，
`report.json` 带有编译 snapshot hash。原 Ramsey 静态讲解脚本继续保留。

### 同页检查 Spectrum 执行代码

新导出的报告增加 **ARTIQ + Spectrum 完整执行流程**，可在同页展开实际 host bridge、
ARTIQ kernel 和 Spectrum adapter 的时钟、触发、上传、step table、arm/ready/完成检查源码。
源码来自导出时的文件快照，带行号与 SHA-256；`runtime/spectrum_runtime.py` 保存适配器快照。
它不是实机运行记录，也不含已经测量合格的校准。

导出的 `GeneratedExperiment.run()` 在 shot 循环之前检查 ARTIQ 目标并调用一次
`initialize_devices()` 初始化 kernel；它与各 shot 的点 kernel 分开。
`_execute_point(point)` 本身是 host 分派方法，选取对应的 `point_N()` kernel。

正常 shot 的顺序是：host 准备 → 全部卡预检 → 必要时逐卡 configure/upload → 选择点入口 → 全部卡 arm →
全部 ready → ARTIQ 点 kernel（准备、同步启动 TTL、按时间线读出）→ 全部卡完成检查 →
host 归档。`configure_for_calibration()` 单独展示，不能每 shot 重写时钟后继续沿用旧校准。
schema 2 runner 在同一 bank 驻留时不重复 DMA；换块时才上传。schema 1 兼容路径保留逐点上传。
`average(scope="global")` 保持旧顺序，cyclic 模式可能重新换入旧 bank；`scope="chunk"` 完成本块平均后换块。
详见 [分块执行](chunked_execution.md)。

概览的逐点卡表是单点程序反解视图。chunk 页面展示实际共享 bank 的 slot、完整 step table、
每点独立入口和最终 `.bin`；每页至多 20 点，概览最多展开前 20 点。
`report.json` 同时记录 host 执行计划和 bank。每个扫描点、每张卡下面还可以展开配置值、来源，
以及由 manifest 展开的 SDK 参数清单；下载为 `point_N/group_N/card_N/sdk_calls.txt`。
该清单供核查，不是省略安全检查后可直接运行的脚本。运行时从 manifest recipe 生成/缓存样本，
报告 `.bin` 是核查副本。`start_step(entry)` 只指定入口；`arm()` 等 EXT0，不直接开始输出。
runner receipt 中的 `uploaded`、`bank_sha256`、`entry_step` 才是执行记录；离线报告不生成实机回执。

首次更新仅重导 Ramsey：

```powershell
uv run --locked python examples/artiq_spectrum/ramsey_long_delay.py
```

已有的其他报告须重新导出才包含此部分，原纯 ARTIQ 示例不受影响。

## 对齐与相位的读法

每 shot 从 F=0、同一逻辑 T0 开始。T0 不一定是第一个 RF 门，也不是卡程序的
sample 0。当前 Ramsey：cooling 从 T0 开始，trigger 在 19.488 us；声明的 400 ns
输出延迟后 sample 0 在 19.888 us，再播放 140 个零样本（112 ns），首门在 20 us。
400 ns 和 112 ns 是本配置模型的结果，不是固定硬件保证。报告时刻相对 T0，
pulse sheet 另有物理准备时间视角。

完整样本相位为 `Phi_osc(t)+s[F+phi_pulse(u)]+offset+calibration`，
默认载波以 sequence 入口为参考；见 [相位模式](spectrum_phase_modes.md)。
每个 pulse 的 `phase_evolution` 记录载波与继承调制状态、解析端点和瞬时频率诊断。
事件详情和 pulse 表中的 `operation_path`、`section_schedule` 记录 Section 层级、
依赖满足时刻、实际起点和栅格等待。数据来自编译结果，没有推断原生 opaque 内部波形。
其中 blocking_resources 列出决定该起点的资源，boundary 区分 layer、全局合流和
此前层边界；start_grid_mu 给出时间网格。完整报告还单独列出最终 DAC pulse 的
重叠区间（最多 1000 个，超出明确标注），不把编译重叠当作实测光场重叠。
Rz(+90°) 对后续门等价于 base phase 减 90°，并持续影响后续门；局部 `phase=-π/2`
只影响该门。`reset_frame` 清整个 F，但不重新定相；`reset_phase` 另清继承调制并
建立新载波参考。末尾 F 不继承到下一 shot，V 仅为最近 reset 之后的 Rz 来源诊断。
零段期间输出零，后续门仍按完整时间线位置计算相位。
表内 `start_phase_deg` 包含载波推进、frame、校准及门首局部调制值。

软件样本一致、真实 ARTIQ 离线编译、SDK/驱动验证、物理幅相验收分别记录。
这些默认示例不操作硬件，也不能证明 <5 ns / <1° 已满足。

## 原生准备尾段的时间轴

`greenmot_ramsey.py` 的 T0 在实际准备尾段开始，前面的可变时长 prefix 不计入相位。
宏观视图用 `native / opaque` 矩形表示固定尾段，随后显示显式准备到控制段等待。
报告给出尾段结束边界、首个真实 Spectrum pulse 的时刻及两者间隔；没有脉冲的点
不伪造首门。原生输出未做波形模拟，不能把矩形当作实际 Zotino/TTL 波形。

真实 GreenMOT 尾段为 15 ms + 16 ns，到最终 LDAC 数字上升沿结束；默认再等 10 µs。
首门在 T0+15.010016 ms，默认 sequence 参考下首相位为0°；显式 shot 参考下才是
108.1728°。时间轴与载波参考分开显示，各实验请以各自报告为准。数字边沿完成不表示模拟磁场已经稳定。
详见 [原生接入和真实准备边界](native_artiq_integration.md)。

大实验使用 [CompilationStore 目录式报告](streamed_execution.md)，按块读取元数据，
按窗口读取最终 packed DAC 样本；保留单 HTML 模式。可链接独立的
[捕获对照](capture_comparison.md)，其中固定 deskew 和原始残差与探索性拟合分开。
