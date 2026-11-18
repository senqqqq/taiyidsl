# realtime 的准确含义与混合 Ramsey

本文保留前一版实现与编译产物的分析记录。2026-11-12 相位修订后，默认 AWG
载波以 sequence 入口为参考，reset_frame 清整个统一 F，新增 reset_phase；下文
旧 H/V 分拆、首门 216° 与 reset 不清 V 的验证只描述历史版本，不代表当前输出。
当前语义见 [相位模式](spectrum_phase_modes.md)，原时基可用 phase_reference="shot" 选择。

更新：封装 DSL 函数、命名 `exp.stage`、已有 GreenMOT 原生 `@kernel` 的区别，
以及本轮修正的 host 生命周期，见 [原生 ARTIQ 接入方案](native_artiq_integration.md)。
已有原生 kernel 通过 `NativeKernelPackage` 的调用代理接入同一顶层 kernel；
直接调用原函数仍不会自动被 DSL 捕获。原生模式的 prefix 在控制 T0 之前，
普通纯 DSL 实验的时间/相位语义保持不变。
若要求真实准备结束到首门固定，声明实际准备尾段为 `timing="timed"`，然后写显式
`delay(...)`；组合器把最后一次 break/epoch 对齐放在尾段之前。GreenMOT 示例已经
这样处理，T0 在真实尾段起点，尾段和 gap 都计入载波相位。

## realtime 是什么

`exp.realtime(timing="strict")` 是一个 DSL 时序作用域：内部操作按编译时已知的
时间关系进入单次 shot 的逻辑时间线。默认串行，只有显式 `parallel` 才产生重叠。
不同信号不会自动并行；并行中一条分支含多个连续操作时，必须用 `serial` 分组。

它不是 Python 实时执行环境、独立线程，也不是 ARTIQ `@kernel` 装饰器。
Python 函数调用、列表计算、文件读取、`time.sleep()` 等仍在构建实验时执行；
函数可以用于封装 DSL，但只有其产生的 DSL 节点会进入硬件计划。
因此不要依靠普通 Python 的耗时、网络访问或动态判断定义 shot 内时序。

进入/退出 realtime 不自动清零时间、重置 frame 或产生硬件 trigger。
当前禁止嵌套 realtime，但不要求每 shot 恰好一个：多个同级区块按外层结构组合，
共享同一个 shot 的调度和 frame。通常将完整 shot 写在一个 realtime 中最清楚。

当前每次 shot 调用一个点 kernel；另有运行开始的设备初始化 kernel。
不是每个 realtime、每个 gate 都生成独立 kernel。
这是当前导出器的选择，不是 ARTIQ 只能使用一个 kernel。多个同 core kernel 可在
设备端互相调用；从 host 分别启动时，也可在保留游标、队列和足够 slack 的条件下
精确衔接。`break_realtime`/`core.reset` 的行为与嵌套关系、分阶段和无缝交接的区别，
详见 [三层概念与 kernel 交接](native_artiq_integration.md)。

`timing="strict"` 表示不自动插空隙、延长门宽或 padding 来掩盖准备时间不足。
纯 ARTIQ 另有显式 `compile(..., allow_quantization=True)`，可按网格取整并记录
diagnostic；所以 strict 不等于覆盖这个开关。默认不启用取整，混合后端直接拒绝
该开关，要求 0.2 ns 公共刻度、0.8 ns DAC 样点及原生 RTIO 网格精确可表示。

## 两种后端的 scope

| 位置 | 适合的命令 | 谁执行定时时序 |
|---|---|---|
| 纯 ARTIQ 的 realtime 内 | play、指定信号 delay、reserve、frame、measure/acquire、serial/parallel/repeat/case | ARTIQ RTIO/DDS，按既有硬件能力编译 |
| 混合 realtime 内、triggered_sequence 外 | 同上，目标是 ARTIQ 信号；也可嵌入一个明确的 Spectrum 块 | ARTIQ 负责这些操作和块的共同启动/结束时间 |
| triggered_sequence 内 | trigplay、两种 delay、frame、serial/parallel/repeat/case | Spectrum 自主执行静态程序，内部操作不产生逐门 ARTIQ 指令 |
| realtime 外 | average/sweep host 结构、配置、模板创建、编译导出，以及 DSL prepare 特例 | 取决于操作性质，不能笼统称为 host 区域 |

`prepare()` 是现有 DSL 的特例：语法要求放在 realtime 外，但它会展开为
shot 内的定时 preparation protocol TTL，占用同一时间线，并进入同一个点 kernel。
它不是普通 host callback。具体 MOT/cooling 函数若展开为定时 `play`，应放在
realtime 内。本例的 cooling 是原生 DDS 定时脉冲占位，不能代替实验室完整冷却流程。

`measure/acquire` 声明定时触发和结果关联，不是在 kernel 内等待相机回图。
数据传输/分析由 host result adapter 的 shot 前后 hook 处理。
`average` 必须是唯一顶层块，`sweep` 在其内部、shot body 外组成嵌套链；当前二者
都是 host execution。`repeat` 是 shot 内有限静态展开，`case` 是根据扫描点编译选择，
不是运行中测量反馈。

Spectrum 块内拒绝 `play/prepare/measure/acquire/reserve`、跨组操作、另一个
triggered_sequence，以及会把 frame 更新外溢到 ARTIQ DDS 的操作。
Spectrum 块内的单参 `delay(duration)` 推进当前串行分支，
不是整卡静音或 parallel barrier。原 `delay(signal, duration)` 的资源语义保留。
原生组合模式另外允许在 realtime 的直接串行 timed 尾段之后、编译控制段之前使用
单参 delay，明确准备结束到控制段的等待；它不把 opaque 原生函数转换成 DSL 波形。

## 示例隔离，不需要 fork

目前不建议建立独立项目 fork。两后端共享 DSL、扫描、frame、结果与校验基础设施；
维护两份副本容易让修复和语义出现差异。示例隔离用目录和显式配置完成：

- 新混合入口：`examples/artiq_spectrum/`。
- 新输出：`output/artiq_spectrum/`。
- 硬件配置明确 `backend="artiq_spectrum"`，使用 `lab_simulated.json` 表明未标定。
- 原有纯 ARTIQ 示例及配置路径保留；原 `examples/spectrum_hybrid.py` 汇总演示也保留。
- 不根据 play/trigplay 自动切换后端；错误组合在编译时拒绝。

未来只有在需要独立发布、长期不兼容的 DSL 或独立维护节奏时，再考虑 fork。
Rabi/Ramsey/echo/spin-locking/allXY 可以在混合目录下各设一个清楚的实验入口。

## 实际 Ramsey 示例

完整、可运行的 DSL：[`ramsey_long_delay.py`](../examples/artiq_spectrum/ramsey_long_delay.py)。
硬件映射：[`lab_simulated.json`](../examples/artiq_spectrum/lab_simulated.json)。

```powershell
uv run --locked python examples/artiq_spectrum/ramsey_long_delay.py
uv run --locked python scripts/spectrum/explain_spectrum_ramsey.py
```

保留原示例三个 dark time：10 µs、1 ms、100 ms。每点两个 200 µs、30 kHz、
归一化幅度0.2的 Square，第二个之前 `rz(q, pi/2)`。这些旋转角尚未标定；
不能因为结构像 Ramsey，就称为已验证的两个物理 π/2 门。readout 只示范10 µs TTL。

生成目录包含完整联合导出、每点每卡 segments/steps CSV、实际 little-endian int16
交织 `.bin`、解码 JSON、PNG/SVG 科学图，以及全部扫描点的 `walkthrough.md`。
RF 曲线从 step table 指向的最终 packed samples 重建，并独立与目标余弦比较到半 LSB。

对于100 ms点，相对逻辑 shot 原点 T0：cooling 0–20 µs，公共 trigger
19.488–19.552 µs；声明400 ns固定延迟后，程序sample 0在19.888 µs，
140点/112 ns前导零后，P1严格在20–220 µs。P2在100220–100420 µs，
readout在100420–100430 µs。trigger比门早发是固定延迟补偿，未在门间插入ARTIQ操作。
这些触发延迟和phase class仍是未实测的示例契约。

100 ms点卡A实际顺序：S0(250144点) → S1(4000点)×31249 → S2(3968点)
→ S3(250048点) → END。S0包含P1及边界零，S3包含P2及边界零；
dark总点数为4+4000×31249+3968+28=125000000，恰好100 ms。
末尾20点/16 ns回零单独记录，不改变门宽或逻辑读出起点。

卡B没有RF门，实际顺序为S0(4000点)×31374 → S1(4160点) → END，
两通道全部零。它仍参与同一组arm/ready/trigger；本例并未演示clock/Rydberg输出。
卡A CH1关闭。两卡各有独立segment编号、内存和step table。

载波参考始终是逻辑T0：P1起相=360°×30kHz×20µs=216°；
P2起相=(360°×30kHz×(220µs+delay)−90°) mod360°。
三点P2分别为234°、126°、126°。后两点相同是整数载波周期，不是重置。
增加1µs等待会使P2起相增加10.8°；空白处没有真实输出或隐藏NCO，
相位由编译器解析计算并写进随后样本。virtual Z没有时长、TTL或独立segment。

这是AWG输出电压的载波参考相位；qubit自由演化、失谐和Ramsey信号是另一层物理问题。
本次仅离线验证，不能据此宣称连接器相位误差已经小于1°。

源码依据：`src/taiyidsl/dsl/experiment.py`、`compiler/scheduling.py`、`spectrum_schedule.py`、
`backends/artiq_spectrum.py`、`spectrum_export.py`、`runtime/spectrum/awg.py`。

## 前导零、virtual Z 与每 shot 初始化的实编译核对

112 ns不是M4i固定参数，也不是每个gate前加的padding。当前配置选择
`trigger_lead_mu=512`（原生1 ns/mu），每卡声明
`trigger_latency_samples=500`（0.8 ns/sample），于是
`prefix_samples=512/0.8−500=140`，对应112 ns。
这个前缀是第一个waveform segment内的140个零码，不是另一个140点segment。
512 ns是示例选择的合法提前量，并非唯一可能值；实际需结合测得的延迟、
触发网格和稳定捕获相位类确定。两卡延迟不同时，前缀可以不同，使RF目标起点一致。

“程序sample 0”是本次回放数据流的第一个样本在输出端对应的模型时刻。
它不是host上传时刻，也不意味着DAC时钟此时才开始运行。本例sample0至139为零码；
sample140开始P1的RF样本。后者是规划的物理AWG输出起点，但目前仍是模型值，
没有测量证明连接器实际严格在该时刻输出；真实零电压偏置和输出响应也需校准。

运行 `uv run --locked python scripts/spectrum/verify_spectrum_phase_equivalence.py`，
得到 `output/artiq_spectrum/phase_equivalence/comparison.json` 及各变体完整导出。
比较覆盖原始10µs/1ms/100ms三个扫描点、最终int16样本逐字节、step table、
原生ARTIQ源码、末尾frame，并额外接第三个pulse检查作用范围。

本例pulse.base_phase=0、binding.phase_scale=1，驱动约定为
`carrier(t−T0) + base + H − V`：

| 第二个门的写法 | 100ms点首RF样本相位 | 门后的V | 对后续门的影响 |
|---|---:|---:|---|
| rz(q,+pi/2); trigplay(q.rf,pulse) | 126° | +90° | 后续同frame门继续减90° |
| trigplay(q.rf,pulse,phase=+pi/2) | 306° | 0° | 仅本次play覆盖base phase |
| trigplay(q.rf,pulse,phase=-pi/2) | 126° | 0° | 仅本次play覆盖base phase |

前者与phase=-pi/2的两卡packed样本、step table、原生ARTIQ源码完全相同，
但完整逻辑IR/frame不同，所以不能称整个DSL完全等价。附加第三个默认phase门后，
100ms点前者仍为126°，单次phase覆盖的版本恢复为216°，样本不同。
若在virtual-Z写法的P2后加`rz(q,-pi/2)`归还V，则第三门样本也恢复相同。
对于非零pulse base phase φ，本次play的相同输出应设phase=φ−θ，而非总是−θ。

原示例没有显式reset_frame，但每个扫描点从新的H=0、V=0计算；每个shot重放
该点程序，并由adapter设置`sequence.start_step(0)`。上一shot的terminal V记录
在产物/结果里，当前实现不会把它带入下一shot。两轮cyclic平均的6次shot对应的
P1均为相同字节、H=V=0、首样本phase216°；这是相对T0经过20µs的载波推进结果。

验证中在Spectrum块开头显式加`reset_frame(q.frame)`，样本、steps、ARTIQ源码
完全不变；在`rz`之后加reset_frame也没有撤销virtual Z。原因是reset_frame仅
清除H，不清除V，也不重设载波epoch。它不等价于“清除全部相位状态”。
目前每shot独立初始化才是保证逻辑起始frame一致的机制；AWG模式本身没有需由此
命令清零的DDS NCO。真实触发重复性、PLL状态与输出相位仍需物理校准和验收。
