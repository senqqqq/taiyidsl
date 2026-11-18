# AOD 搬运：IZ、move 与 Spectrum 原生 DDS

混合后端中，A 卡继续用 AWG 输出全局 RF，B 卡用 AWG 输出 clock/Rydberg，
C 卡用原生 DDS 控制交叉 X/Y AOD。C 卡在搬运结束后保持终点频率和囚禁幅度，
在门操作、等待、shot 间隔和 A/B 换 bank 时继续输出。

本功能的离线轨迹、SDK 命令记录与 mock 验证都不是原子搬运成功的测量证据。
真实时钟、触发延迟、坐标标定、阱深、运动限制与存活率需要实验室另行验收。

完整例子在 [transport_dds.py](../examples/artiq_spectrum/transport_dds.py)：

```powershell
uv run --locked python examples/artiq_spectrum/transport_dds.py --report
```

默认输出目录为 `output/artiq_spectrum/transport_dds/`。例子先定义命名 pulse 和
轨迹，再构建实验；编译、导出与硬件执行分开。

## 卡模式与接线

在项目自己的 `spec_db` 中按卡指定模式。它不是 Spectrum 官方数据库格式。
descriptor 继续负责选择仪器、输出通道和触发线，不再重复指定模式。

```python
spec_db = {
    "A": {"serial_number": "12345", "mode": "awg"},
    "B": {"serial_number": "12346", "mode": "awg"},
    "C": {
        "serial_number": "12347",
        "mode": "dds",
        "dds_routing": "16/5",
        # 示例值，必须替换为本机时序声明和测量结果。
        "dds_trigger_latency_ns": 560.0,
    },
}
```

省略 `mode` 保持旧的 AWG 行为。当前 DDS 目标是 M4i.6631-x8 + DDS20；连接时
只读检查实际卡型号、DDS 模式能力、固件选件、core 数量和队列容量；首次运行
准备阶段配置并读回 core 路由，通过检查后才启动输出。填写配置不会赋予硬件
尚未安装的 DDS 选件。`dds_trigger_latency_ns` 是独立的 DDS 时序参数，不能
使用 AWG 的 `trigger_latency_samples` 替代。

DDS20 的双通道卡有固定的 core 路由：

| 路由 | 通道 0 | 通道 1 |
| --- | --- | --- |
| `20/1` | core 0–19 | core 20 |
| `16/5` | core 0–7、12–19 | core 8–11、20 |

不能任意分成 10/10。双轴示例使用 `16/5`，X/Y 轴与通道的对应由 transport
配置显式声明。C 的 tone 是 AOD 资源，不需要伪造 `q.aod` 或量子门 frame。

## 站点、轨迹与起点

`IZ` 第一版保存两个 `Position`。`iz[0]` / `iz[1]` 返回不可变 `SiteRef`；
原子身份始终由 Qubit UID 表示，不随站点改变。

```python
iz0 = IZ("iz0", sites=(destination_0, destination_1))
profile = MoveProfile.minimum_jerk(segments=32)

# 写在实验的 realtime / triggered_sequence 时序内。
move([q0, q1], to=[iz0[0], iz0[1]], duration=100 * us, profile=profile)
play(q0.clock, clock_pulse)  # clock_pulse 已在实验正文前定义。
move([q0, q1], to=[q0.position, q1.position], duration=100 * us, profile=profile)
```

`duration` 必填；默认轨迹是线性，默认 `settle=0`。`settle` 是显式的额外稳定
等待时间，不由编译器偷偷加入。目标可为 `SiteRef` 或带相同坐标系的 `Position`。
同位置搬运保留所请求的保持时间。

`duration`、`settle` 和搬运起始时刻必须对齐 6.4 ns；包含 DDS 的触发区域总长
及 DDS 触发前缀也须对齐。编译器不会静默拉长这些时间。轨迹内部的归一化采样
时刻才会量化到该格点，并使用量化后的实际分段时长计算斜率；采样点重合时拒绝。

起点在编译时从内部位置表解析：第一次使用实验快照里的初始位置，后续使用
前一次搬运的终点。`move()` 不修改外部 `q.position`。这使零次 repeat、静态
case、Section 多次插入和同一实验重复编译具有一致含义。第一版要求每个 shot
显式返回初始布局；软件重置位置表不能代替物理搬回。

提供线性、minimum-jerk 分段近似和自定义归一化时间/位移采样点。自定义曲线沿
起终点连线前进；多段空间路径使用多次 move。DDS 硬件执行线性斜率，平滑曲线
会被离散为有限段，因此 `segments` 是近似精度和命令预算之间的选择，不代表
硬件执行了精确的连续多项式。

```python
linear = MoveProfile.linear()
custom = MoveProfile.from_samples([(0.0, 0.0), (0.2, 0.1), (0.8, 0.9), (1.0, 1.0)])
```

每个采样点是 `(归一化时间, 归一化位移)`，时间严格递增、位移不减，首尾固定
为 `(0, 0)` 和 `(1, 1)`。这些对象在 move 调用前命名，与命名 pulse 的使用方式一致。

## 共享 tone 与照明范围

`LabConfig.transport` 保存站点、轴标定、tone/core 分配、原子与 tone 的绑定、
幅度补偿和运动限制。轴标定为单调查表；两个点即线性标定，不在有效范围外
外推。坐标使用米，频率使用 Hz。

可传入 `TransportConfig` 或相同字段的参数表。`x_calibration` / `y_calibration`
每行为 `(position_m, frequency_hz[, amplitude_scale])`，幅度 scale 乘以
`initial_amplitudes` 中对应 core 的基准幅度。例如两个原子共享 Y core：

```python
transport = TransportConfig(
    card="C",
    x_calibration=((0, 70e6), (40e-6, 90e6)),
    y_calibration=((0, 70e6), (15e-6, 85e6)),
    bindings={
        "q0": {"x_core": 0, "y_core": 8},
        "q1": {"x_core": 1, "y_core": 8},
    },
    initial_amplitudes={0: 0.08, 1: 0.08, 8: 0.1},
    zones=(iz0,),
)
lab = LabConfig(setup, qubits=(q0, q1), transport=transport)
```

以上标定表、幅度与位置均为示意。实际文件应声明实验室坐标系、位置/频率容差和
最小间距；示例内的默认容差不能替代实验校准。

交叉 AOD 中，共享同一 X 或 Y tone 的原子会沿相应轴联动。批量搬运必须包含
全部受影响的已登记原子，并为共享 tone 给出一致的轨迹。遗漏 spectator、
tone 交叉/合并、冲突目标和超出标定范围的请求会被拒绝。第一版只支持串行
move 和一个显式协调批量 move；不支持在 parallel / ASAP 中重新排序搬运。

在 B 卡光学通道声明 `coverage={"zones": ["iz0"]}`，编译器根据当时的站点
占用推导作用对象。同一束光仍只产生一份波形，校准来源与所有受影响对象分别
记录。A 卡的全局 RF 覆盖不因原子移出 IZ 而变成局域控制。

## 持续 DDS 输出与运行边界

首次运行在 C 卡建立初始 hold，随后保持卡处于运行状态。每个 shot 先在保持
期间装入完整命令包，再由 ARTIQ 外触发启动；中间使用 DDS timer 执行斜率，
终帧固定终点频率、将频率/幅度斜率归零，并切回 CARD trigger 等待下一轮。

- `session.run()` 返回、shot 间隔和 A/B bank 更新都不停止 C。
- 关闭连接会话时停止 C；异常结束后位置标为未知，不自动重试搬运。
- 第一版使用 SINGLE 完整预装；命令超出容量会报错，不依赖 shot 内主机补包。
- 容量预算包含初始 hold，并保守预留 4 条路由/divider 配置写；上传前读取驱动
  软件队列计数再检查上限。这不宣称所有固件都会把这 4 条配置写排入 FIFO。
- DDS 更新/定时格点是 6.4 ns；timer 的 83.2 ns 下限还必须同时满足完整命令帧
  的准备预算。另加 13 个命令周期的 guard 是软件的保守约束，不是已验证的最高更新率。
- 完成由本轮预期 trigger count 和错误状态判断。队列为空可能只表示终帧已经
  进入 shadow registers，并不意味着该帧已经执行。

复杂轨迹会报告时间、频率及斜率量化误差。幅度补偿需要幅度斜率时，必须在 C 卡
配置中提供正数 `dds_amplitude_slope_step_per_s`，来源应是实际能力探测结果；
运行器会核对这一精度。恒定幅度轨迹不需要这个字段。

每次 move 结束显式固定频率和幅度。若下一次 move 紧接着开始，同一帧的频率
固定会占用一次更新；编译器按扣除 6.4 ns 后的有效 ramp 时间计算斜率，并记录
`ramp_update_delay_mu`。普通曲线中间节点只更新斜率，不反复固定频率。
位置容差用于站点匹配、硬件频率实现和路径余量；不代表 minimum-jerk 的有限
分段近似与理想连续多项式处处相差小于该容差。

C 不保存软件绝对相位账本，不参与 qubit frame 或 A/B 相位相干契约。保留硬件
DDS 的默认连续行为即可；搬运命令不主动 reset 或 phase jump。这与“输出幅度
可以在等待期间归零”是不同的问题，仍承载原子的 tone 必须持续输出。

`compiled.export_report(...)` 的 `waveform.html` 和 `compiled.waveform_simulator(...)`
自动将C的DDS频率/幅度与A/B的AWG波形放在同一横轴，按tone标识和分行显示。
分块报告也支持此入口；无需实验自行编写绘图代码。DDS非零hold延伸到已知控制窗口边界，
不为未知shot间隔指定时长。具体交互见 [编译查看器](compiled_viewers.md)。

报告展示的是计划位置、频率/幅度轨迹和实际导出的 DDS 命令；这些不是从 DAC
样本重建的 AWG 波形，也不是原子位置的实测结果。

## 官方依据

- [M4i.66xx/96xx 官方手册](https://spectrum-instrumentation.com/dl/m4i_m4x_66xx_96xx_manual_english.pdf)：模式能力、core 路由、命令、定时和状态。
- [Using the DDS Mode](https://spectrum-instrumentation.com/download_files/application_notes/PN_Using_DDS_Mode_on_AWG.php)：连续输出、相位行为、分段自定义斜率及触发。
- [官方多通道 core 路由示例](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/07_dds_multiple_carriers_channels.py)。
- [官方复杂 ramp 示例](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/12_dds_complex_ramps.py)。
- [官方重复 pattern 示例](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/15_dds_repeated_patterns.py)：本项目终帧保持囚禁幅度，不照搬示例中的关断。
