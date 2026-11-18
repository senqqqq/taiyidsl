# 剩余工作与后续优先级

核对日期：2026-11-17。本文列出混合控制、搬运与读出教学后的实验室接入工作。

本清单描述当前能力边界，不提供新的执行证据。教程复现命令见[教学入口](README.md)，
每次执行在本机 output 中产生独立记录；这些生成文件不随源码分发。历史编译和
回归结果不能代表本次克隆、修改或实验室设备已经通过。

当前共享入口是[设计决定](../design_decisions.md)、[架构](../architecture.md)和
[合作者指南](../development.md)。旧的 H/V 相位分拆及早期“尚未实现”描述不能
脱离当前实现作为待办；新克隆不依赖本机历史日志或旧计划文件。

剩余工作主要是实机验收、实验室接入，以及当前接口有意保留的能力边界。

## 尚未完成的验收

| 优先级 | 工作 | 当前状态与完成条件 |
| --- | --- | --- |
| P1 | 浏览器实际查看 | 已有静态链接与脚本检查；此前浏览器查看受权限和 URL policy 阻止。仍需实际打开报告，检查切换 chunk、窗口波形、缩放、源码链接与长时间浏览。 |
| P1 | 多卡与混合控制实机验收 | TTL 同刻排程和离线校验已经提供；实际偏斜、采样同步、SDK/驱动、DMA、step table、ready/re-arm、RTIO 提交能力及模拟幅相仍未测量。不能声称达到 `<5 ns / <1°`。 |
| P1 | 真实准备与探测接入 | GreenMOT 支持指定外部源码，默认是原生 fixture；RB 的四个默认组件也是 TTL 时序 fixture。需绑定实验室源文件、设备配置、模拟稳定时间、相机取图和分类流程。 |
| P1 | 物理 CZ 与校准 | RB 演示 CZ 由两路普通 clock pulse 构成，没有证明实现纠缠门。需替换为真实相互作用门体，并提供单比特、双比特及相位补偿的实测校准。 |
| P1 | 探测器持久化与恢复 | RunStore 的软件和 mock 恢复路径已有；真实相机文件与 point/shot/attempt 的关联、崩溃后的数据找回、状态不明时的处理仍需端到端验收。 |
| P2 | GreenMOT 上游来源身份 | 先前来源通过浏览器转录并进行 AST 对照，尚未验证远端原始文件的字节身份。接入实际实验时应固定可复核的源码版本与来源。 |

依据：[当前设计边界](../design_decisions.md)、
[原生组件](../native_components.md)、[GreenMOT 接入](../native_artiq_integration.md)、
[RB 示例边界](../randomized_benchmarking.md)、[捕获对照](../capture_comparison.md)。

静态报告检查、mock 执行、真实 ARTIQ 离线编译和物理输出验收是不同证据。
离线编译通过不会证明原子已准备好、相机已取得图像或 CZ 已实现。

## 当前限制

### 磁盘编译控制了哪些内存

`compile_stream()` 已经逐点写盘、逐块导出，并按 hash 保存唯一最终样本。其预算计量
点/native/diagnostics 的规范化数据字节和受控样本工作区，默认 256 MiB **不是进程
RSS 上限**。普通 `compile()` 仍保存全部编译点。

以下数据仍会随实验规模增长：

- 全局点号、chunk、文件哈希及唯一样本索引；完整索引在打开 store 时驻内存。
- 输入 DSL、轴值和实验级 metadata，以及编译和序列化时产生的副本。
- 当前 RB builder 保存的全部随机电路描述，以及已展开到 case 中的门体；因此长序列
  和更多随机电路会增加输入本身的内存。
- 生成公共初始化程序时暂存的各块设备映射与初始化片段。

Python 对象开销、上述全局数据和外部编译缓存不在托管预算内。因此可以说“逐点落盘、
逐块处理并限制托管工作区”，不能说“任意大 RB 的端到端内存恒定”。

其他边界包括：stream 当前只用于 `artiq_spectrum`；所有块完成后才能运行；不支持
中断编译续编；输出目录必须新建或为空。自动分块仍由用户指定一个 host sweep 轴，
保留每个轴值的全部内层组合，不拆单 shot。手动分块超预算会报错，不暗改边界。

依据：[磁盘编译、执行记录和分页波形](../streamed_execution.md)、
[分块顺序](../chunked_execution.md)、
[当前 RB builder](../../examples/artiq_spectrum/randomized_benchmarking.py)。

### 原生固定控制边界

新组件顺序固定为 `prefix* → timed* → DSL 控制 → suffix*`。原生调用是 realtime 的
直接子节点，不支持放进 Section、parallel 或 triggered_sequence，也不支持门间
任意交错。timed/suffix 返回时检查实际游标，缺时或超时都报错，不补等待。

精确的准备到首门间隔由实际固定尾段和其后的显式 delay 界定，不能从可变 prefix
的返回时刻推断。提前 trigger/preload 若侵入不透明尾段会被拒绝。GreenMOT 的结束
定义是最后 LDAC 数字边沿，默认 10 µs 等待未经模拟稳定时间标定。

GreenMOT 保留旧 `NativeKernelPackage` 接口，使用普通 compile；流式 native 路径
要求 `NativeProgram`。新组件的 initialize/finalize 是会话一次，after_shot 是每 shot；
旧 package 的 finalize_method 仍保留原有每 shot 语义。

依据：[原生组件](../native_components.md)、[GreenMOT 接入](../native_artiq_integration.md)。

### 门、测量与恢复的含义

- `PairCalibration` 保存依赖、参数、补偿和来源，并检查版本一致性；文件存在或状态
  字段本身不是物理校准证明。具体 CZ 门体负责实际相互作用及相位补偿。
- 独立控制 frame 不自动推导多能级量子相位，也不解除同一 qubit 的资源约束。
- GreenMOT 原生 readout 不自动创建 DSL acquisition handle 或 ResultSet。相机触发、
  图像传输、分类与持久提交需要明确衔接。
- RunStore 区分硬件完成与数据提交。unknown 默认停止，重试/跳过必须明确；恢复不
  承诺物理 exactly-once，重启进程也不恢复原子相干态。
- 理想 RB recovery 验证电路代数；合成衰减数据验证分析流程，两者都不证明演示波形
  的实际门保真度。P00 必须来自同 shot 联合结果，loss/invalid 策略必须明确。

依据：[参数与操作](../qubit_parameters_and_operations.md)、
[RB 与 CZ IRB](../randomized_benchmarking.md)、[持久采集与恢复](../streamed_execution.md)。

## 此前明确延期或未纳入本次范围

- **更广的 DDS 用途**：交叉 AOD 的原生 Spectrum DDS 搬运已实现；任意量子门的
  Spectrum DDS 后端、与 AWG 共用绝对相位账本不在当前范围。
- **跨控制器相干交接与多次启动**：同 qubit 跨 DDS/AWG 相干控制、同组每 shot 多个
  独立启动区域和实时反馈未纳入当前范围。
- **更广的结构能力**：独立 circuit 编译器、center 对齐和任意 Section 重绑定明确
  不在最新扩展计划中。

这些事项应作为新增设计任务评估，不与尚未完成的验收混为一类。早期计划中提过但
后来已经实现的 continuous/carry 相位、reset_phase、多组件 native、自动分块、
磁盘编译、完整双比特 Clifford RB 和 Spectrum DDS 搬运，不应继续列为未实现。

依据：[混合控制](../spectrum_hybrid.md)、[DDS 搬运](../transport_dds.md)、
[当前设计决定](../design_decisions.md)。

## 建议实施顺序

1. **完成教学与浏览器查看。** 用小规模、可重复的 GreenMOT Ramsey 和 2q RB/IRB
   串起参数、映射、原生声明、序列、编译、报告和结果分析。教程始终标明当前数据与
   验证层级，并实际检查报告交互。
2. **完成实验室接入与物理验收。** 固定源文件、device_db、SDK、固件及校准版本；
   接入真实 detector adapter 和 capture；逐项验收准备边界、多卡同步、模拟幅相、
   CZ 与恢复行为。
3. **按真实规模优化编译。** 先测量输入 AST、全局索引、托管计量与 RSS 的增长，再
   优先考虑惰性生成 RB 门体、范围或磁盘索引、共享实验级 metadata、边读边合并
   初始化信息。避免只缩小 chunk 却把输入本身的增长留在内存。
4. **改善大规模运行与维护。** 优化全局点到块/局部点的查找，评估编译续编需求；
   整理当前文档入口，保留历史证据但避免教程引用过期语义。
5. **按实验需求启动延期功能。** 只有具体实验确实需要时，再设计多次启动、反馈、
   跨 DDS/AWG 相干控制或更广的 DDS 门控制，并为其单独定义时间与相位验收。

后续验证按实际改动范围选择。说明文档只核对内容和链接；可执行 notebook 检查其
离线执行路径；共享调度、时间单位、相位或 IR 修改按受影响语义与后端选择检查，
不自动扩大为全量回归。提交和推送遵守 [CONTRIBUTING](../../CONTRIBUTING.md)，
不将历史测试数字作为新结果。
