# 公共 API 与扩展入口

此索引用于选择当前推荐接口，不是把全部源码模块冻结为永久兼容层。
原有顶层公开符号继续保留；新增实验优先采用下表写法。
包归属与阶段状态见[源码分层](source_layout.md)。旧路径保持轻量兼容，库内部使用规范位置。
`taiyidsl` 的普通导入不连接仪器，也不要求安装 ARTIQ 或 Spectrum SDK。

## 推荐入口

| 使用场景 | 导入路径与主要接口 | 说明 |
| --- | --- | --- |
| 实验正文 | `taiyidsl`：`Experiment`、`play/delay/trig/acquire`、`Square`、`FlatTopGaussian`、单位 | 命名 pulse 与实验正文分开；[编写方式](experiment_authoring.md) |
| 函数式上下文 | `taiyidsl`：`experiment/average/sweep/realtime/triggered_sequence` | 装饰器与显式 `exp.xxx()` 共用语义；[触发与装饰器](authoring_and_triggers.md) |
| 参数与接线 | `taiyidsl`：`Qubit`、`Position`、`DeviceSetup`、`LabConfig`、`OpticsConfig` | qubit 角色参数与物理设备配置分离；[配置入口](device_setup_descriptor.md) |
| 可选实验模板与 RB | `taiyidsl`：`rabi/ramsey/randomized_benchmarking`、`ramsey_experiment`、`generate_rb_sequences/rb_sequence_section` | 既有实验构造与理想序列入口；教程仍直接展示实验正文，专用实验放实验室包 |
| 操作复用 | `taiyidsl`：`Section`、`section/add/layer/barrier`、`qubit_operation`、`QubitOperations`、`PairCalibration` | [段与操作](sections_and_operations.md) |
| 原生组件 | `taiyidsl` 或 `taiyidsl.native`：`NativeModule/NativeStage/NativeProgram` | 新包用 `NativeModule.from_package`；[生命周期](native_components.md) |
| 结果与图像 | `taiyidsl` 或 `taiyidsl.results`：`ResultSet`、`ImageResults` | 采集索引、状态、原始帧和分析；[成像](results_and_imaging.md) |
| 读出扩展契约 | `taiyidsl.runtime.readout`：`ResultAdapter/PersistentResultAdapter/RecoverableResultAdapter` | 类型协议，不要求继承；[适配器](readout_adapters.md) |
| 大扫描与持久执行 | `taiyidsl`：`CompilationStore`、`compile_stream`、`StreamingRunner`、`RunStore`、`ShotContext`、`ObservationCommit` | [分块与恢复](streamed_execution.md) |
| AOD 搬运 | `taiyidsl`：`IZ`、`SiteRef`、`MoveProfile`、`TransportConfig`、`move` | [设备范围与闭合规则](transport_dds.md) |
| 编译扩展 | `taiyidsl` 或 `taiyidsl.backends`：`Backend/BackendCapabilities/register_backend/get_backend` | 显式注册，无插件扫描；[后端契约](backends.md) |
| Spectrum host 集成 | `taiyidsl.runtime.spectrum.awg`：`SpectrumAdapter/SpectrumRunner/SpcmAdapter`；`taiyidsl.runtime.spectrum.dds`：`SpcmDDSAdapter` | 硬件适配器是受配置约束的集成入口，须分别验收 |
| 报告与查看器实现 | `taiyidsl.reporting` 下对应模块 | 实验作者通常使用 compiled 的方法；扩展展示时见[源码分层](source_layout.md) |

实验作者仍可使用上述顶层入口。维护库内部代码时使用实际所有者：声明与组合在
`taiyidsl.dsl` 的子模块；配置模型在 `taiyidsl.config`；编译入口与产物在
`taiyidsl.compiler.pipeline/artifact`，磁盘编译与读取在 `compiler.streaming/store`。
`taiyidsl.dsl.sections.Section` 是唯一 Section 类型，compiler 没有第二套定义。
ARTIQ 目标实现归 `taiyidsl.backends.artiq` 的子模块，混合目标归
`taiyidsl.backends.artiq_spectrum`；各自包入口按需解析后端符号。实验构造归
`taiyidsl.experiments` 的子模块，这些内部归属不要求实验作者改掉顶层导入。
`ramsey` 保留原有 Raman 模板含义，`ramsey_experiment` 是 native 准备／读出加 RF
Ramsey 的构造器；目录调整不会把两者合并成语义不同的同一实验。

`lab.compile(exp)` 是通常的编译入口；`compiled.export()`、`export_report()`、
`visualize()`、`waveform_simulator()` 从同一已编译计划导出和检查。
`lab.connect()` 是显式连接入口，和离线编译分开。已有运行器的具体能力并不相同，
尤其不能从 `ImageResults` 的存在推导出 session 已支持相机取数。

## 继续支持的其他写法

例如旧实验使用 `from taiyidsl.sections import Section`，仍会取得同一个类。
新实验使用 `from taiyidsl import Section`，修改组合逻辑请打开
[dsl/sections.py](../src/taiyidsl/dsl/sections.py)。下表中原先平铺的转发文件
集中在 `_compat/`，只供旧导入按需解析。开发新代码直接使用表中的推荐入口。

| 接口 | 保留范围与使用建议 |
| --- | --- |
| `compile_experiment(exp, setup, mapping, ...)` | 低层显式编译入口；实验室配置集中时通常用 `LabConfig` |
| `reference_setup`、`reference_lab`、`initialization_lab` | 离线示例配置，不替代真实接线或标定 |
| `NativeKernelPackage`、其 `from_package()` | 单包接入仍有效；`finalize_method` 每 shot，不能与新组件的会话 finalize 混淆 |
| `taiyidsl.native_components` 等旧原生模块、平铺报告模块 | 转发到 `native/`、`reporting/` 的同一实现；新内部开发使用新位置 |
| `taiyidsl.session/stream_runtime/run_store/readout`、旧 Spectrum runtime 模块 | 转发到 `runtime/`；顶层 `StreamingRunner/RunStore/ShotContext/ObservationCommit` 继续保留。`lab.connect()` 仍是连接入口 |
| `taiyidsl.analysis/imaging/rb_analysis/rb_results` | 转发到 `results/` 中同名模块；`ResultSet/ImageResults` 顶层入口不变 |
| `taiyidsl.dsl`、`taiyidsl.compiler` | 从单文件改为 package，原公开值与编译入口继续导出同一对象 |
| 平铺 `authoring/sections/operations`、配置和排程模块、`streaming` | 转发到职责包；新内部代码直接使用所有者；拆分多个职责的旧模块按符号转发 |
| `taiyidsl.hardware.reference_setup` | 实现归 `taiyidsl.lab` 的离线参考配置入口，旧路径继续可用 |
| `taiyidsl.artiq/artiq_square/artiq_ram/artiq_polar`、`submission/waveforms/polar_validation` | 按符号转发至 `backends.artiq` 的生成、契约、验证等所有者；生成与验证不再混放 |
| `taiyidsl.backends.artiq`、`taiyidsl.backends.artiq_spectrum` | 原模块改为 package；后端类与既有导入继续解析到同一所有者 |
| 根层 `spectrum_schedule/spectrum_phase/spectrum_export/spectrum_banks/spectrum_dds/spectrum_waveforms` | 转发至 `backends.artiq_spectrum` 子模块；AWG `validate_program` 归 `validation_awg`，回放归 `awg` |
| `taiyidsl.ramsey/cooling/templates/rb/rb_dsl` | 转发至 `experiments.ramsey/cooling/templates/rb_sequences/rb_sections`；旧 templates 数学符号指向共享 Clifford 实现 |
| `Protocol`／`ProtocolStep` | 既有触发协议命名保留；新说明优先使用 `TriggerSequence`／`TriggerStep` |
| `CompiledExperiment.export_artiq()` | ARTIQ 兼容入口；不转换已经由其他后端编译的事件 |
| 旧式 `Qubit`／`QubitStore` 参数对象 | 既有只读存储与版本行为保留；新实验用角色参数，日常编辑无需先引入 Store |

这些接口不是仅因列在此表就被弃用。改变语义或移除入口必须单独说明迁移策略。
教程允许采用更新的导入与调用方式，稳定目标是实验结构与语义，而非旧调用文本。

## 内部实现与数据边界

实验室模块不要依赖 `_builder`、`_NativeCall`、私有 `_descriptor`、native audit
的内部结构，或直接修改 setup 的私有字段。通过声明对象、配置工厂和公开编译入口
扩展；需要新的公共能力时先给出具体使用场景与验证。

按职责选择内部模块：ARTIQ `codegen*` 只生成程序，`verification*` 执行并核对，
`export` 编排发布。AWG `validation_awg` 的数学期望样本与 `awg` 回放分开；DDS
`dds.validate_dds_program` 按原意重建并校验完整性，不代表独立物理输出验证。
纯 Clifford 算法位于 `experiments._single_qubit_clifford/rb_sequences`，不依赖
DSL；`rb_sections/templates` 单向使用它们。`spectrum_calibration` 仍保留根层，
是分析与运行时解析共享的功能，不是待删除的兼容文件。

`Node` 树、排程 helper、导出模板和 viewer payload 是内部实现。已公开的
`ScheduledEvent`／`PhysicalPlan` 供后端实现使用，仍带有目标时基和资源语义。
`compiled.ir` 是审查记录，当前没有公开的序列化 IR 再编译入口；换后端需从原实验
重新编译，不复用旧目标的 physical events。

导出 JSON、bank、DDS 程序、RunStore 记录及源码哈希是可复核产物。不要手改它们
以绕过完整性校验；从实验与配置重新生成。需要变更产物结构时维护版本/读取兼容性，
并覆盖实际消费者。详细设计见[当前决定](design_decisions.md)和[架构](architecture.md)。
