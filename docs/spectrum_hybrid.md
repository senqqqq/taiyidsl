# ARTIQ + 双 M4i.6631-x8 混合 AWG 实验

本阶段实现 `artiq_spectrum` 后端：ARTIQ 保留 cooling、TTL readout 和实验框架，
一个明确的 `triggered_sequence` 区域编译成两张 Spectrum 卡的完整静态程序。
两卡在 host 阶段准备完成后，ARTIQ 发一次同步启动（公共 TTL 或分别给两卡的两路 TTL），卡内随后按采样时间线播放。
`trigplay()` 是编译指令，既不是每门调用 Python SDK，也不代表每门发一个 TTL。

普通 `play()` 现也可在该区域内使用，旧 `trigplay()` 保留。双卡线路映射见
[触发配置](authoring_and_triggers.md)；新增 [Section / 高层操作](sections_and_operations.md)、
[相位模式](spectrum_phase_modes.md) 和 [分块驻留运行](chunked_execution.md)。
下文的单线、epoch/local 示例继续支持，默认串行行为不变。

现有 `artiq` 后端继续可用。本文讲解 A/B 的 `awg_sequence` 路径；新增的第三卡
原生 DDS20 搬运路径见 [IZ / move](transport_dds.md)。卡模式由 `spec_db` 按卡
声明，可在同一同步组混用 AWG 与 DDS。普通导入、编译和导出不打开仪器。

运行完整离线示例：

```powershell
$env:UV_CACHE_DIR = "C:/AIproject/taiyidsl/.uv-cache"
$env:PYTHONIOENCODING = "utf-8"
uv run --locked python examples/spectrum_hybrid.py
```

默认生成 `output/spectrum_hybrid/summary.json` 和八个案例目录。
每个目录包含联合 `manifest.json`、`compiled.json`、`sequence_report.json`、
host 入口 `experiment.py`，以及保留原生 kernel 的 `artiq/experiment.py`。
导出过程会离线验证原生 ARTIQ 事件和 Spectrum 实际采样回放；这不是一次真实
ARTIQ 工具链编译、Spectrum 驱动执行或示波器验收。

可以选一个案例，也可以显式生成耗时较长的核自旋门压力程序：

```powershell
uv run --locked python examples/spectrum_hybrid.py --case ramsey_delay
uv run --locked python examples/spectrum_hybrid.py --case rb_like_500
uv run --locked python examples/spectrum_hybrid.py --case three_channel --nuclear-stress
```

`--output DIRECTORY` 改变输出目录。`--nuclear-stress` 额外编译、采样并导出
100/500 个 200 μs 核自旋门；默认运行只给出这两种长门程序的保守容量预测。
预测明确标成 `analytic_upper_bound_not_a_compiled_program`，不冒充实际回放校验。

示例里的通道配置如下。A卡只启用A:0，A:1明确关闭；B卡启用两个通道。所有频率都是
AWG 端口直接输出的 RF 频率，不是 IQ 基带或外部 LO 的偏移量。

| 逻辑信号 | 设备/物理位置 | 波形 |
| --- | --- | --- |
| `q.rf` | `nuclear_rf`，A:0 | 30 kHz，200 μs，Square 或 FlatTopGaussian |
| `clock` | `clock_rf`，B:0 | 150 MHz，300 μs，FlatTopGaussian + 半正弦相位 |
| `rydberg` | `rydberg_rf`，B:1 | 200 MHz，400 ns，FlatTopGaussian + cubic 相位演示 |
| `cooling` | 原生 ARTIQ `cooling_dds` | 80 MHz，20 μs 演示脉冲 |
| `readout` | 原生 ARTIQ `readout_ttl` | 10 μs TTL 窗口 |

`SIM_NUCLEAR` 和 `SIM_AOM` 只用于离线配置；它们不是仪器序列号。
20 μs cooling 不是完整 MOT/cooling 配方，readout TTL 也不生成相机图像或测量结果。
实际实验应保留已有原生制备与读出流程，替换示例的这两个占位操作。

所有 Gaussian 例子的 `sigma=T/20`、`cutoff_sigma=4`、`sample_interval=0.8*ns`。
`FlatTopGaussian` 的第一个参数是 **flat_duration**，因此总时长为
`flat_duration + 2*cutoff_sigma*sigma`。200 μs 核门对应 flat=120 μs、sigma=10 μs；
300 μs clock 门对应 flat=180 μs、sigma=15 μs；400 ns Rydberg 门对应
flat=240 ns、sigma=20 ns。不能把总时长直接填进 flat_duration。

```python
clock_pulse = FlatTopGaussian(
    180 * us,
    15 * us,
    cutoff_sigma=4,
    amplitude=0.35,
    frequency=150e6,
    sample_interval=0.8 * ns,
    phase_modulation=HalfSinePhase(math.pi),
)
rydberg_pulse = FlatTopGaussian(
    240 * ns,
    20 * ns,
    cutoff_sigma=4,
    amplitude=0.3,
    frequency=200e6,
    sample_interval=0.8 * ns,
    phase_modulation=PolynomialPhase((0, 0, 3 * math.pi, -2 * math.pi)),
)
```

局部调制使用整门坐标 `u=t/T`。`HalfSinePhase(a)` 表示 `a*sin(pi*u)`；
`PolynomialPhase((c0,c1,c2,...))` 表示 `c0+c1*u+c2*u**2+...`，单位为弧度。
上面的 cubic 仅用于演示，不代表已经确定或标定的 Rydberg gate 协议。
最终实数 RF 采用 `amplitude * envelope * cos(carrier_phase + local_phase)`。

完整配置使用 `SpectrumCard`、`SpectrumOutput`、`SpectrumGroup` 和
`SpectrumConfig`，通过 `DeviceSetup(..., spectrum=config)` 接入
`LabConfig(..., backend="artiq_spectrum")`；见示例的 `offline_lab()`。
`SpectrumGroup.outputs` 的键是物理设备名，`SignalMap` 将 DSL 信号绑定到这些设备。
`SpectrumCard` 明确声明采样率、参考时钟、输出量程、活动通道和卡序列号。

```python
group = SpectrumGroup(
    trigger_device="awg_trigger",
    cards={
        "A": SpectrumCard("SIM_NUCLEAR", (0,)),
        "B": SpectrumCard("SIM_AOM", (0, 1)),
    },
    outputs={
        "nuclear_rf": SpectrumOutput("A", 0),
        "clock_rf": SpectrumOutput("B", 0),
        "rydberg_rf": SpectrumOutput("B", 1),
    },
    calibration={"status": "unmeasured", "revision": "offline-example"},
)
config = SpectrumConfig(
    {"qubit_awg": group}, signal_frames={"clock": None, "rydberg": None}
)
```

`q.rf`、`q.raman` 默认关联所属 qubit frame；自定义 Spectrum 信号必须在
`signal_frames` 中明确关联 qubit UID，或明确写 `None`。
示例的 `clock` 和 `rydberg` 没有关联核自旋的 frame，因此 `rz(q, ...)`
不会偷偷改变这两路信号。如果物理模型需要共享 frame，应修改这个显式映射。

实验书写保持原有嵌套结构：

```python
with exp.average(2), exp.realtime():
    play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
    with exp.triggered_sequence(group="qubit_awg"):
        trigplay(q.rf, Square(200 * us, amplitude=0.2, frequency=30000))
        delay(100 * ms)
        rz(q, math.pi / 2)
        trigplay(q.rf, Square(200 * us, amplitude=0.2, frequency=30000))
        rz(q, math.pi / 3)
    play("readout", Square(10 * us))
```

每个 group 每个 shot 目前只允许一个完整区域；区域必须位于 `realtime()` 内，
不能嵌套。区域内使用 `play`/`trigplay`、等待、frame 操作和正常的 serial/parallel；
原生 ARTIQ 操作留在区域外。一个参数扫描点或 shot 的准备完成后，触发整个区域。
host 扫描位于 realtime 外，幅度、相位、频率和时长等按扫描点编译；不承诺恒定
host shot 周期或跨 shot 的载波相位连续性。

`delay(duration)` 是区域内的纯时间推进，不要求指定一个虚构 TTL 资源。
`delay(signal, duration)` 保留指定信号的等待语义。普通串行顺序跨信号依然串行；
两个输出要重叠，可显式使用 `exp.parallel()` 或选择 `section(schedule="asap")`；多个先后操作应装进一个
`exp.serial()` 分支。并行在最长分支的结尾汇合，同一物理通道重叠会拒绝。

```python
with exp.triggered_sequence(group="qubit_awg"), exp.parallel():
    trigplay("clock", clock_pulse)
    with exp.serial():
        delay(100 * us)
        trigplay("rydberg", rydberg_pulse)
```

`rz(q, theta)` 是零时长逻辑 frame 操作，令统一 F 减 theta，后续驱动使用 `base+F`。
末尾 Rz 即使没有后续 RF 也保留在 logical IR、区域的 `terminal_frames` 和
`compiled.frame_phases(point)` 中。virtual_z_frames 仅保留最近 reset 后的 Rz 来源。
`reset_frame(q.frame)` 清整个 F；`reset_phase(q.frame)` 另清继承调制并重建载波参考。
Rz 不能插在一个正在播放的门内部；有歧义的跨并行分支 frame 更新会拒绝。
允许的门内硬件 `shift_phase/reset_frame` 通过样本位置上的 phase step 表达，
不会把 Gaussian 或局部 phase modulation 拆成重新归一化的多个脉冲。

默认采用 sequence 入口下的完整 RF 采样相位。cooling 等前置操作不计入，序列内部
Ramsey 暗时间计入。phase_reference="shot" 可选择原共同 T0 参考。
为了触发补偿加入的零前缀不改变该逻辑相位。
这保证编译的脉冲不会仅因缓存、切段或前缀发生一次额外 phase reset。
真实输出相对 Kasli 时钟的相位仍需要实机 epoch 与延迟校准。

一个逻辑程序并不等于一份覆盖全部 wall time 的巨大数组。程序 JSON 保存
pulse 目标、segment recipe、SHA-256 和有限 step 链，采样数据按 tile 生成。
同卡两通道按 sample-major 顺序打包为 little-endian signed int16，范围检查
拒绝削顶；相位变化已经包含在真实 RF 样本中，没有假设硬件 step 自带独立
幅度或相位缩放。只有实际 packed bytes 相同的段才能去重。

两活动通道的B卡每段至少192 samples，单活动通道的A卡最小384；长度为32的倍数。
400 ns=500 samples可以位于一个较长tile内部或跨tile，门本身不必作为独立段。
默认tile上限500000 samples，短门压力案例采用4000 samples。
只有同卡所有活动通道都为零的等待才压缩为有限零段循环；另一通道持续输出时，
不能把整段等待误当成零。末尾追加硬件需要的零尾并分别记录logical/physical时长，
不移动gate边界。100 ms Ramsey例子不会保存100 ms的逐采样零值。

容量按每卡共享4 GiB检查，包含最多65536个段、4096个step、每step有限循环
`1..2**20-1`。段数分配向上取2的幂，所有slot等分板卡内存，所以仅把各段字节数
相加不足以判断可用性。报告中的 `resident_bytes` 是唯一packed数据总量；
`allocated_bytes` 是“最大段 × 分配slot数”的容量下界，不等于实际传输字节数。
`slot_bytes` 是当前等分方式下每slot可容纳的最大字节数。

默认100/500门压力案例在200 MHz输出400 ns Square，逐门改变轴相位，不插gap。
这些是固定seed的随机phase words，没有Clifford抽样、反演门、存活概率测量或
统计拟合，**不能称为统计合格的 randomized benchmarking 或保真度结果**。
Rabi、Ramsey和echo同样只是采集前的实验时序模板；示例幅度不是已标定的π/2、π门。

对于200 μs核门，默认报告另外给出不依赖去重的保守估算：100门的A卡等分容量
需求上界64000000 bytes，500门为256000000 bytes；B卡空闲用零段循环。
实际字节、分段、步数和相位变体应以显式运行 `--nuclear-stress` 后的导出报告为准。

2026-11-10本机另行实际运行了100/500个200 μs核门的编译与联合导出；两案均通过
离线回放验证，每案一个公共trigger，相邻门的最小/最大gap均为0 samples。
下表来自`output/spectrum_hybrid/nuclear_stress_report.json`，耗时是本次本机记录，
不是硬件吞吐指标或跨机器基准；默认八例的summary另行保留。

| 核门数 | A唯一段/step | A驻留bytes | A等分容量需求bytes | 编译/导出及验证 |
| --- | --- | --- | --- | --- |
| 100 | 35 / 49 | 34000320 | 64000000 | 2.79 s / 4.97 s |
| 500 | 67 / 245 | 66000320 | 128000000 | 17.34 s / 17.36 s |

两案的B卡均为2个零段、2个step，驻留32640 bytes；500门对应100 ms完整时间线，
但B卡无需保存约500 MB的逐采样零值。实际去重降低了A的容量需求，预测仍保留
不假定去重的上界；没有把某次随机phase word的复用率推广为任意门序列的保证。

本示例配置假定两卡接收同一125 MHz参考时钟与公共触发，没有配置StarHub。
`trigger_latency_samples=500`、`trigger_lead_mu=512`、`epoch_grid_mu=32`
等默认值是**未测量占位契约**，不是该卡或实际接线的测量结果。
group中的 `_mu` 字段使用原生ARTIQ machine unit；示例为1 ns。
混合调度内部使用200 ps，AWG样本格为800 ps，导出的原生ARTIQ参数仍使用其原始mu。
不落在要求格点上的时序会拒绝，不静默四舍五入或补gap。

硬件部署先安装可选Python SDK与Spectrum原生驱动；离线示例不依赖它们：

```powershell
uv sync --extra spectrum --locked
```

SDK安装不等于硬件标定。真实运行要显式创建 `SpcmAdapter` 和 `SpectrumRunner`，
为导出的host入口设置 `spectrum_runner`，并配置原生ARTIQ device_db及结果适配器。
不要把SIM序列号替换成真实号后直接把本示例标成measured。

```python
from taiyidsl.runtime.spectrum.awg import SpcmAdapter, SpectrumRunner

# real_group、measured_sessions 和 generated_experiment 由实验室启动器提供。
adapters = {
    name: SpcmAdapter(card.serial_number, clock_session_id=measured_sessions[name])
    for name, card in real_group.cards.items()
}
generated_experiment.spectrum_runner = SpectrumRunner(
    adapters,
    cache_dir="output/spectrum_cache",
    max_cache_bytes=256 * 1024**2,
)
```

adapter构造不打开卡；probe或显式配置才访问驱动。runner先验证全部卡的身份、
功能、内存、时钟及校准，再配置/装载全部卡、arm并等待全部ready，最后调用
一次ARTIQ shot入口。等待的是“已arm并等待外部触发”，不是代表播放完成的
`M2STAT_CARD_READY`。某卡失败时停止已经触及的卡，不继续触发实验。
`allow_unverified=True`仅允许非硬件mock适配器，不能绕过真实adapter的校准要求。

校准记录属于 `group.calibration`，包括revision、卡fingerprint、clock session，
每通道的整数sample shift，以及按精确载频区分的 `frequency_points`。
表内每个频点保存gain、phase_offset_rad和相位不确定度；频率必须唯一精确匹配，
不会拿150 MHz记录插值或冒充200 MHz记录。频率扫描增加新载频时，也需要对应测量。
卡、firmware、量程、接线或时钟session改变后，应使原记录失效。
`configure_for_calibration()`是显式硬件动作，会建立新的clock session；
正常runner不会为了“修好”时钟而悄悄重锁PLL。

离线校准工具在 `taiyidsl.spectrum_calibration`，不会控制示波器或卡。
`load_capture_npz(path, metadata)`读取`voltage`、`envelope`、
`reference_phase_rad`三个数组，调用`fit_rf_trace()`；参考phase必须已经包含
Kasli epoch、目标载频和局部调制。metadata保留通道、载频、trigger/rearm/lock ID、
采集时间、独立测得的包络delay、固定deskew与测量不确定度。

固定补偿按两步得到，避免把同一延迟补偿两次：

1. 先独立测量并deskew原始包络延迟，将`delay_ns`、`timing_uncertainty_ns`和
   `sample_rate_hz`放入`timing_calibration["A:0"]`等条目。
2. 按`round(-delay_ns*sample_rate_hz/1e9)`先平移完整RF pulse，再采集phase/gain记录。
   每条记录带`sample_shift_applied`和`carrier_frequency_hz`；相位校准针对平移后
   剩余的误差，包含尚未消除的分数sample延迟所产生的载波相位。

```python
from taiyidsl.spectrum_calibration import calibrate_channels, analyze_measurements

# 输入来自真实独立测量；不是把仿真波形或离线示例声明标成measured。
profile = calibrate_channels(
    post_shift_records,
    revision="lab-controlled-revision",
    card_fingerprints=measured_card_fingerprints,
    clock_session_ids=measured_sessions,
    timing_calibration=measured_envelope_timing,
)
report = analyze_measurements(
    independent_validation_records,
    profile,
    corrections_applied=True,
    required_channels=("A:0", "B:0", "B:1"),
    lock_calibrations=profiles_by_lock_id,
)
```

`calibrate_channels()`产出估计与`physical_acceptance="not_verified"`。
将新profile放入配置后必须重新编译，程序携带`calibration_sha256`；runner对照
`calibration_fingerprint(profile)`检查，禁止用旧样本搭配新校准记录运行。
`resolve_channel_calibration()`同时校验频点与`phase_reference="after_sample_shift"`。
生成 `hardware_fingerprint(contract, observed)` 时，`contract` 必须来自导出
manifest 的完整 `groups[*].cards[card_id].contract`，不能只使用 `SpectrumCard`
自身字段。完整契约还绑定 trigger device、触发宽度、ARTIQ 原生 tick 和 epoch
grid/offset；改变触发捕获相位类或声明输出延迟后，旧校准不能继续使用。

验收应同时测量Kasli参考、ARTIQ触发和三个AWG输出，先做示波器通道deskew，再确认
所有允许触发phase class及重启/PLL重锁后的行为。`select_trigger_window()`从多卡
记录中选共同稳定窗口的中心，返回的是candidate；需要另外扫外部触发延迟并验证
实际setup/hold裕量。用统一epoch检查相位，不能逐shot扣掉一个拟合phase来隐藏抖动。

`analyze_measurements()`保留拟合phase误差，报告跨记录峰值、RMS、包含不确定度的
phase界以及同时采集的通道对skew。完整性检查要求每通道至少10000次触发、
100次stop/rearm、单lock至少30分钟数据，所有通道对至少10000次同时采集，
以及3个分别校准并绑定session的lock。缺失覆盖保持not_verified，复用校准数据
或只作解析预测也不能通过物理验收。

单个capture的常数phase拟合不能独自证明调制脉冲内部的瞬时相位误差。
高频通道必须另外提供预先定义窗口的完整证据：

- `measurement_scope="phase_windows"`；
- `phase_window_bounds_deg`：每个窗口的绝对phase残差加测量不确定度；分析器再加入固定校准不确定度；
- `phase_window_alignment_removed=False`：没有逐窗口扣除自由phase/时间对齐；
- `phase_windows_cover_effective_envelope=True`、`0 < phase_analysis_threshold_fraction <= 0.1`：覆盖整个有效包络；
- `rf_residual_rms`、`rf_residual_rms_limit`，采用同一电压单位，阈值由实验预先声明，没有默认通过阈值；
- `rf_residual_reference="unaligned_target"`：完整RF残差对照未逐record对齐的目标波形。

只有实际30 kHz载频允许`measurement_scope="known_waveform_fit"`，并要求
`phase_was_subtracted_for_acceptance=False`及同样的完整RF残差证据。这是短记录内
已知波形的相位参数估计，不声称从六个周期可靠提取每时刻的瞬时phase。
`fit_rf_trace()`会保留这种fit scope与未对齐目标的RF残差，但不会自动生成高频
窗口证据；调用方必须用已声明的测量分析方法提供它们。`fit_residual_rms`只作诊断，
不能代替验收中的未对齐残差。局部phase或完整RF越限返回failed，缺少证据返回
not_verified，报告的`waveform_evidence_missing`解释缺项。
目标仍是包络skew<5 ns、相位误差<1°；离线检查与measured字段本身不构成实测证明。

两卡三路量子门全 DDS 尚未实现。当前 DDS20 仅用于第三卡的连续 AOD tone 搬运，
不把普通量子门 `play` 转成 DDS 指令，见 [transport_dds.md](transport_dds.md)。
计划先限制为Square和显式时间校验。
DDS20的6.4 ns参数时间格意味着400 ns请求必须拒绝，DDS演示将使用403.2 ns；
本阶段AWG仍保持精确400 ns。Gaussian和连续局部调相不自动降级为近似DDS指令。
DDS 搬运已检查频率/斜率量化、命令准备预算和 SINGLE 预装载容量；SHIFT/JUMP
门相位控制不在搬运接口范围。DDS 实机时序与输出仍需独立验收。

硬件能力依据可查[官方M4i.66xx数据表](https://spectrum-instrumentation.com/dl/m4i66_datasheet_english.pdf)、
[官方硬件手册](https://spectrum-instrumentation.com/dl/m4i_m4x_66xx_96xx_manual_english.pdf)和
[官方spcm Python源码](https://github.com/SpectrumInstrumentation/spcm)。

本次软件交付验证（2026-11-10）：全量 **946 项 pytest 通过**，其中包含实施前
782 项既有测试；冻结基线未改写。Ruff lint/format、wheel/sdist 构建通过。
8 个默认示例共 13 个扫描点，以及 100/500 个 200 µs 核门实际编译/导出通过。
10 份 native ARTIQ 产物（包含 Rev8/Rev9 的 Square/ASF/POWASF 并存）共 25 个
kernel，以及实际顶层 bridge 的 2 个继承 kernel，通过 ARTIQ 9.9664 / rv32g
真实离线编译。使用合成 device_db，未执行 kernel 或连接仪器。

完整软件报告在 `output/spectrum_hybrid/verification_summary.json`；
原生编译报告位于各案例的 `artiq_compile/compile_report.json`，桥接编译报告位于
`bridge_compile/compile_report.json`。独立的 SDK fake 测试不等于原生驱动验证。
硬件接线、共同参考的电气兼容性、随机触发跳变、连续运行稳定性以及
<5 ns / <1° 输出验收仍为 **未验证**。校准工具已提供，但本次没有实测数据。

后续教学入口：[realtime 语义与实际 Ramsey 分段](realtime_and_hybrid_ramsey.md)。
新增混合示例集中在 `examples/artiq_spectrum/`，生成内容在
`output/artiq_spectrum/`；已有示例路径保留，不建立项目副本。
