# 合作者开发指南

TaiyiDSL 的公共入口、配置和扩展应让实验代码直接表达时序。新增功能前先读
[公共 API 索引](public_api.md)、[当前设计决定](design_decisions.md)和
[贡献约定](../CONTRIBUTING.md)。本页介绍选择哪一层；具体契约由对应专题说明维护。

源码目录、依赖方向和当前迁移阶段见[源码分层](source_layout.md)。`Section` 的定义与
组合 API 属于 DSL；编译器中的 `section_schedule` 只处理它的排程，不重复定义 Section。

## 先保持实验作者的上手路线

入门以 [Plain Ramsey](tutorials/ramsey_clock_plain.ipynb) 为基准，按
**device db → descriptor → LabConfig → Qubit → native ARTIQ → experiment definition
→ compile & visualization → connect to real hardware** 展开。
先展示可编辑的数据库、接线与参数，再展示完整实验函数；学会后才把重复配置搬到
实验室自己的包。操作版只封装短小的门，不把整段实验藏进 helper。

实验作者使用 `from taiyidsl import ...` 等[推荐公共入口](public_api.md)。内部重组
服务维护者，不要求教程跟着导入 `compiler`、`runtime` 的私有实现。优先普通字典、
显式物理参数、命名 pulse 和顺序可读的实验函数；不为少写几行引入动态加载、命名空间
包装、扫描轴自动发现或通用配置框架。NumPy/Matplotlib 可用于分析和作图，数值辅助
与实际时序分开。多组件、恢复、分块与插件设计不作为第一个 Ramsey 的前置知识。

保持设备事实、接线、qubit 标定与实验控制量各有一个清晰来源。连接与执行放在末尾的
显式步骤；离线 Run All 不运行硬件。相机适配与 native 生命周期详解放在对应专题，
不要把完整底层框架挤进实验正文。教程入口与生成器清单见[教程说明](tutorials/README.md)。

## 开发环境与代码归属

从源码工作区运行 `uv sync --locked`；教程另加 `--group notebooks`。
库与离线测试不要求安装 ARTIQ、Spectrum SDK 或连接仪器。
Node.js 用于查看器脚本检查，版本及 CI 约定见 CONTRIBUTING。
ARTIQ 使用独立环境，安装和解释器配置见[工具链指南](artiq_toolchain.md)。

| 放置位置 | 内容 |
| --- | --- |
| TaiyiDSL 主库 | 可复用语言、配置模型、编译契约、后端、数据模型和通用组件 |
| 实验室自己的 Python 包 | MOT/冷却/态制备类、相机适配、设备配置工厂及专用操作 |
| 实验室本地配置与数据目录 | 实际地址、接线、校准、设备凭据和原始测量数据 |
| 本仓库 examples | 可运行的离线接入示例及明确标注的模拟配置 |

按[最小实验室扩展示例](../examples/lab_extension/README.md)安装独立示例包后运行：

```bash
uv run --no-sync python -m my_lab.demo
```

再把适合自己实验室的部分放入独立包。实验室包显式导入 TaiyiDSL，通过配置或普通
函数传入设备依赖；无需注册自动发现插件。厂商 SDK 应只在实际适配器连接时加载。

## 选择扩展入口

| 需求 | 首选方式 | 详细说明 |
| --- | --- | --- |
| 新实验或扫描 | 普通函数接收 qubit、命名 pulse、控制量，返回 `Experiment` | [实验编写](experiment_authoring.md) |
| 复用门与小段时序 | `Section`、`qubit_operation`、`QubitOperations` | [段与操作](sections_and_operations.md) |
| MOT、冷却、态制备 | ARTIQ 类＋`NativeModule`＋`NativeProgram` | [原生组件](native_components.md) |
| 拍照、标量读出 | 主机 result adapter；触发在 native 或 DSL 内显式声明 | [读出契约](readout_adapters.md) |
| ROI、阈值、占据分析 | `ImageResults` 与 `ResultSet` | [结果与成像](results_and_imaging.md) |
| AOD 搬运轨迹 | `MoveProfile`、`TransportConfig`、`move` | [DDS 搬运](transport_dds.md) |
| 新硬件执行模型 | `Backend` 或既有硬件 adapter 边界 | [后端接入](backends.md) |

### 编写实验与可复用操作

先定义命名 pulse，再写实验函数；编译、显示和硬件执行放在函数外。优先显式参数，
避免让实验依赖可变全局状态或隐藏设备查找。不要为节省几行代码把完整实验藏入大型
helper。`@experiment` 是返回完整实验的可选写法，`qubit_operation` 则返回一段
`Section`；有活动上下文时操作会自动插入，不能再 `add` 一次。

新式 `q.parameters` 可编辑，但已建好的实验与段保留快照。命名 pulse 的幅度和
时长已保存时，修改校准后要重建 pulse，再重建实验。函数按明确单位计算失谐；
不要同时在频率与相位中重复补偿同一个 detuning。

### 接入原生 ARTIQ 组件

将 host 声明与 ARTIQ 实现分开：声明模块导入 `NativeModule/NativeStage/NativeProgram`，
kernel 文件才导入 `artiq.experiment`。使用 `NativeModule.from_package(...)`
收集静态本地依赖并固定源码；`root` 是包含顶层实验室包的目录，`entry` 是
`module:Class`。原文件保持在实验室包中，导出时校验来源。动态 import 不能伪装
成已发现的依赖，外部依赖由 ARTIQ 环境安装。

声明实际占用的设备和共享总线；多个组件共享资源时，显式列出所有参与者及唯一
初始化所有者。`build_kwargs` 是固定 host 配置，stage 参数才可绑定 sweep。
`host_prepare` 在 host 准备，`initialize/finalize` 每会话一次，`after_shot` 每 shot。
旧 `NativeKernelPackage.finalize_method` 仍是每 shot，迁移时不能直接照搬名称。
含自行 `core.reset()` 的既有初始化方法也不能机械迁入组件；新组件要求 core reset
由根初始化管理。现有 GreenMOT fixture 使用兼容单包入口，先核对实际调用图再迁移。

当前边界是 `prefix* → timed* → DSL 控制 → suffix*`，不支持在门间任意插入
native kernel。精确尾段声明 duration 和可核验的结束边沿；不得靠补等待掩盖时长
不符。真实光学稳定时间仍需标定，TTL fixture 不表示已经冷却或制备原子。

### 接入相机与恢复数据

相机触发和取得图像是不同步骤。普通导出在 host 调用两参数的
`before_shot(point, shot)`、`after_shot(point, shot)` 和 `finish()`；持久执行的
`StreamingRunner` 可以传入 `context`、`run_store`，并要求有效的 observation 提交。
两种入口的返回值与恢复能力详见读出契约，不把它们强行做成同一个运行器。

先 arm，再执行触发，最后读取对应帧；始终保留 point/shot/attempt 的关联。
保存原始数据后提交其资产引用和哈希，不能仅凭“kernel 已返回”声称数据已保存。
空位是有效分类结果，缺帧是无效采集。恢复只找回原 attempt 的数据；硬件状态
unknown 时默认停止，不为弥补文件缺失自动重做实验。

`HardwareSession.run()` 当前没有 detector adapter 注入入口，对 acquisition 会拒绝；
本指南不承诺 `lab.connect().run()` 已经能够拍照。

### 修改执行与数据代码

运行器放在 `runtime/`，已有观测的处理放在 `results/`。新增运行能力前先明确资源
归属与失败位置；不要让分析函数负责重打 shot，也不要让 journal 代替相机取得数据。

| 所有者 | 持有及释放的资源 | 边界 |
| --- | --- | --- |
| `HardwareSession` | worker 进程、管道、接收线程和日志 | 显式连接；通信失败使 session 失效；关闭逐项尝试且幂等 |
| `runtime.worker.Worker` | 自己创建的 ARTIQ DeviceManager、Spectrum adapters/runner | 部分连接失败也清理；单个设备关闭失败不阻止其他设备关闭 |
| worker 的一次程序加载 | 生成模块和本次新增的 import 路径 | 构造或执行异常都撤销；不卸载仍可被会话对象引用的 native 模块 |
| `StreamingRunner` | 当前块程序、会话 native context、临时缓存替换 | 尝试 finalizer、还原缓存、卸载生成模块和记录会话完成；不关闭调用者传入的 managers、adapter 或 RunStore |
| 相机 adapter 的调用者 | 相机连接、缓冲和保存目录 | 使用外层 context manager／finally；`finish()` 仅表示正常结束，不是必达析构 |
| `RunStore` | SQLite 连接、执行锁与提交记录 | 调用者负责 `close()`；验证资产和 observation，再标记提交，不执行硬件 |
| `results` | 标量/图像、快照、归档和分析 | 不拥有仪器；存档失败保留原文件，缺帧与空位保持区别 |

`runtime._cleanup.run_cleanup` 仅处理异常传播：全部动作依次尝试，已有主异常时追加
清理错误 notes；没有主异常时抛第一个清理错误。具体停止条件、DDS hold 和恢复策略
由各运行器决定，不放入通用 helper。成功释放 AWG bank 不停止持续 DDS hold；异常
路径仍 abort。把两者统一成“每次结束都 close”会改变实验语义。

持久执行依次分为恢复检查、所有块目标预检、换块与一次初始化、单次 attempt、会话
清理。恢复检查按 execution order 在该 shot 的硬件动作之前完成；不是预先执行全 run
的所有恢复回调。不要提前写 `data_committed`，也不要在清理失败时把 unknown 改成
可自动重试。改动这些边界时选择对应的失败/恢复定向用例，详见
[执行记录](streamed_execution.md)；改图像归档时选择缺帧、完整性及失败保旧文件用例。

### 扩展 AOD 或后端

新轨迹优先用 `MoveProfile.linear/minimum_jerk/from_samples` 与显式标定。
现有 transport 针对交叉 X/Y AOD 与 Spectrum DDS；普通 `play` 和 qubit frame
不会自动成为 DDS 搬运命令。更换设备执行模型才需要后端/runtime 设计。
不要把厂商 SDK 调用直接写入 notebook 的门序列。

后端保持无状态，设备时钟、延迟、映射和校准放在配置中。改变排程、量化或导出语义
时更新后端版本，并给出独立于生成器的事件/样本检查。不能通过插 gap、修改包络、
静默取整或降低采样率把不支持的请求变成另一个实验。

### 修改后端生成、验证和实验库

先按行为找到所有者，再决定是否需要新模块：

| 修改内容 | 实现归属 |
| --- | --- |
| ARTIQ 物理展开／设备限制 | `backends.artiq.backend` 与 `_preparation/_square/_ram_*` 等设备路径模块 |
| ARTIQ 输出源码 | `backends.artiq.codegen*`；共有目标记录在 `_contracts/polar_contracts` |
| ARTIQ 执行对照 | `backends.artiq.mock_drivers/verification*`；`export` 连接生成、验证和发布 |
| Spectrum 排程／相位 | `backends.artiq_spectrum.backend/scheduling/phase` |
| AWG 样本与有限回放 | `backends.artiq_spectrum.awg`；独立数学期望在 `validation_awg` |
| DDS 命令与 bank | `backends.artiq_spectrum.dds/banks`，实机执行仍在 `runtime.spectrum` |
| 可复用实验和理想序列 | `experiments.ramsey/cooling/templates` 与数学模块分别维护；实验室专用流程留在实验室包 |

生成器不导入 mock 模型或验证器。验证入口可以按需生成默认源码，但预期输出仍由
独立驱动模型或数学公式重建，不能直接复制生成器的决定充当正确性证明。
DDS 现有校验复用构造算法重建程序，适合检查原意、命令、限制和 digest 一致；
不要把它写成独立物理输出模型。AWG 验证使用独立期望样本公式，但共用回放工具。

后端命名空间按需解析符号；读取样本／命令的内部代码直接导入其所有者，避免把数据
处理绑到后端类的初始化。旧平铺模块继续兼容，新内部实现不从兼容转发导入。
`spectrum_calibration` 服务分析与运行时解析，`spectrum_identity` 服务纯身份计算，
两者继续处于根层；不要为了清空根目录制造反向依赖。

`experiments._single_qubit_clifford` 和 `experiments.rb_sequences` 保持纯数学，
不依赖 DSL、编译或设备。`templates/rb_sections` 消费确定的序列再构造 DSL；
修改 Clifford 算法时检查目录顺序、seed、recovery 和独立矩阵结果，不能仅比较两条
都调用同一新算法的路径。移动实验函数可以改变来源路径，不能改变物理事件或相位。

## 配置快照与编译修改的归属

| 时点 | 保存什么 | 后续修改的影响 |
| --- | --- | --- |
| 编辑 qubit／实验室配置 | 可编辑角色参数、接线与标定 | 仅用于以后新建的实验或编译 |
| 创建 Experiment／Section | qubit 和通用参数的独立只读快照；保留参数子类型 | 不随原字典或 qubit 更新；已有命名 pulse 的幅度/时长也不自动更新 |
| 开始编译 | 复制 backend、setup、mapping 和实验树，按原顺序验证 | 后端准备只作用于编译副本，不修改用户设备配置 |
| 逐点编译 | 当前轴坐标、逻辑/物理事件、采集签名与计划位置 | 空间状态属于当前排程，不写回 `q.position`；每个 point 保持既定 frame 规则 |
| 发布产物 | 来源、参数、配置、host 计划与内容身份 | `CompiledExperiment` 用完整性检查拒绝事后篡改；磁盘 store 仅在完整完成后发布 |

`config` 管模型、校验、快照与静态解析；`lab.LabConfig` 是应用入口，选择后端并显式
调用编译或连接。不要为了目录名称把这些应用动作放回 `config`。通用参数快照允许
Quantity/Fraction，qubit 扩展字段有自己的 JSON 约束，两套冻结规则不能机械合并。

配置 JSON、源 IR、最终产物的编码各有契约。尤其 Fraction 在配置和源 IR 中转为数值，
通用产物序列化可以保存分子/分母；重构时不得换一个 serializer 就静默改变 schema/hash。

编译流程由 `compiler.pipeline` 编排，准备输入、逐点编译、产物组装各自分离。
内存与磁盘编译共用 schedule → 采集签名检查 → lower → native 绑定的顺序，内部
point sink 只决定如何保存已编译点，不定义另一套排程。新增校验要保留其发生时点，
尤其 inactive case 的参数绑定、全扫描的采集一致性和未完成 store 的拒绝规则。

Section 的声明与组合只在 `dsl.sections`。通用放置算法通过 scheduler 的
`section_grid()`、`signal_grid()`、`signal_frame()`、`frame_owner()` 和 `time_origin`
读取时基与资源语义。新目标在自己的 scheduler 中提供这些信息，不能让通用 Section
算法反向 import 具体后端，也不能通过改网格、加 gap 或重置 frame 来迁就排程。

## 提交与验证

为每次修改说明“改变的行为 → 受影响调用链 → 必要断言”。优先正常路径、必要边界
和有风险的兼容路径；纯文档修改只检查内容与链接。保留已有有效测试，不重复验证
同一契约。完整 CI 与本地定向检查是不同层次。

| 修改／检查点 | 验证范围 |
| --- | --- |
| 说明文字与链接 | 内容和链接检查，不启动测试或构建 |
| 一项行为或接口 | 受影响调用链的定向测试，修改文件的 lint/format |
| 教程可执行内容 | 同步生成器，受影响 notebook 在全新 kernel 离线执行 |
| 已约定的跨阶段验收 | 四份主要教程、Ramsey 两版语义对照及一次完整离线测试／CI，记录最终源码身份 |
| native 调用图或生成程序变化 | 受影响入口的真实 ARTIQ 离线编译 |

跨阶段检查点用于发现模块各自通过后仍可能遗漏的集成问题，不变成日常每次编辑的
固定流程。已有检查只有在相关输入改变、出现失败或仍有具体疑点时才重跑。

公共接口或示例迁移可以更新 import、构造与调用方式，但保留实验意图、教学结构、
时序、相位和采集索引。修改生成教程时同步生成器及 notebook，使用全新 kernel
验证受影响教程；不把旧 notebook 文本原封不动作为兼容标准。

格式改变可能影响源码行号及 provenance hash。比较物理事件、最终样本、触发、
扫描顺序与结果索引，不能只比较文件名或盲目更新基线。原生调用图或生成 kernel
变化后，只编译受影响入口；复用证据必须与最终源码和 ELF 身份一致。

PR 写清行为变化、迁移方式、实际检查和未验证项。分开报告离线软件检查、真实 ARTIQ
编译、浏览器实显和实机验收；不能把其中一项通过推导成其他层次通过。
