# TaiyiDSL：中性原子实验编写与协作

用 Python 描述 pulse、实验时序、扫描和重复采样，离线编译后查看最终波形与执行计划，
再通过显式连接运行到实验室设备。实验正文保留物理顺序，配置与分析放在正文外。

**当前定位：实验室协作版，默认离线。** 已有 ARTIQ 9 / AD9910 / TTL 与
ARTIQ＋Spectrum AWG/DDS 后端。离线软件验证、真实 ARTIQ 编译与实机验收分别记录；
历史编译结论只适用于当时的源码和配置。RTIO 吞吐、板卡同步、原子处光场时序及相机
取数仍需实验室验证。示例参数不是实测标定，TTL fixture 不表示已经加载、冷却或探测原子。

## 从 Ramsey 开始

先打开 [Plain Ramsey 与 clock_ramsey](tutorials/ramsey_clock_plain.ipynb)，
再看相同实验的 [qubit_operation 版](tutorials/ramsey_clock_walkthrough.ipynb)。
两版遵循同一条上手路线：

| 顺序 | 这一层回答什么问题 |
| --- | --- |
| device db／spec db | 哪些设备和板卡，使用什么地址与时钟？ |
| descriptor | 物理线路接到哪些逻辑信号，哪些输出共享？ |
| LabConfig | 使用哪套 setup、qubit 和离线／实机模式？ |
| Qubit 与命名 pulse | 标定参数是什么，这个门用哪个已定义 pulse？ |
| native ARTIQ | 准备和读出调用哪些既有 kernel，边界与时长是什么？ |
| experiment definition | 按什么顺序打 pulse，扫描什么量，重复多少次？ |
| compile & visualization | 实际排程、波形、触发和导出程序是否符合实验意图？ |
| connect to real hardware | 显式使用真实配置连接，取得执行记录后释放会话资源 |

`from taiyidsl import ...` 仍是推荐的实验作者入口。源码分成 `dsl/config/compiler/runtime`
不要求 notebook 跟着导入内部实现。先命名 pulse，再写普通实验函数；实验时序保持
可见，不需要先学习配置框架、动态导入或大量 Python helper。操作版仅把可复用门
封装成短小函数。修改幅度或时长后重建命名 pulse，再构建实验、编译并刷新报告。

后续按实验需求选择：

- [¹⁷¹Yb global-RF tune-up](tutorials/yb171_global_rf_tuneup.ipynb)：二维 Rabi/Ramsey、相干与损失对照、CZ 回波/Bell/SSB 和波形候选；另见[文献与实验对应](tutorials/yb171_benchmarking_references.md)。
- [Clock Ramsey＋IZ 往返搬运](tutorials/ramsey_clock_transport.ipynb)：显式 move，查看 A/B AWG 波形、C DDS 命令与位置变化。
- [综合进阶参考](tutorials/hybrid_from_scratch.ipynb)：多组件、RB、分块、联合读出和持久执行；写第一个 Ramsey 不需要这些内容。

## 安装与运行

在源码 checkout 根目录执行：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/ramsey_clock_plain.ipynb
```

项目使用 uv 管理的 CPython 3.12.15，版本在 `.python-version`，依赖锁定在 `uv.lock`。
只开发库时运行 `uv sync --locked`。教程使用仓库内的示例与 native 源码，因此从
checkout 运行；仅安装 wheel 不包含课程。具体生成、全新 kernel 执行和输出位置见
[教程说明](tutorials/README.md)。默认执行所有代码单元不会连接仪器。

普通离线编译不需要 ARTIQ 或 Spectrum SDK。实机使用独立的
[ARTIQ 工具链](artiq_toolchain.md)、原生驱动和实验室配置。可选 Python SDK 通过
`uv sync --locked --extra spectrum` 安装，当前锁定并检查的版本为 `spcm 1.13.4`。
环境本体、工具链和生成报告不上传源码仓库，详见[安装与发布范围](repository_contents.md)。

## 从教程到实验室

先在 notebook 中看懂并编辑设备库、descriptor、qubit 和实验函数，复用时再保存到
实验室自己的文件或包。实际地址、接线、标定和相机配置由实验室维护；通用 DSL、
编译契约和数据能力进入主库。起步示例见[实验室扩展包](../examples/lab_extension/README.md)。

现有 Ramsey fixture 保留 `NativeKernelPackage.from_package(...)`，其初始化包含
`core.reset()`，finalize 每 shot 执行。新建独立 MOT／冷却组件推荐
`NativeModule.from_package(...)`＋`NativeProgram`；其 initialize/finalize 是每会话
钩子，不能通过换类名直接迁移。详细契约见[原生组件](native_components.md)。

真实连接放在课程末尾，使用实验室核对过的设备配置、时钟会话与独立解释器。
`lab.connect()` 建立连接，`session.run()` 才执行；返回的 receipt 是执行记录。
相机触发不自动取得图像，`HardwareSession.run()` 当前没有 detector adapter 注入入口。
需要拍照时参阅[读出 adapter 契约](readout_adapters.md)，分别处理触发、取帧与提交。

## 专题与开发入口

| 需求 | 阅读入口 |
| --- | --- |
| 实验函数、并行、门复用 | [实验编写](experiment_authoring.md)、[Section 与 operation](sections_and_operations.md)、[角色参数](qubit_parameters_and_operations.md) |
| 接线与原生程序 | [DeviceSetup 与 descriptor](device_setup_descriptor.md)、[native 接入](native_artiq_integration.md) |
| 波形与相位 | [Spectrum](spectrum_hybrid.md)、[相位模式](spectrum_phase_modes.md)、[编译查看器](compiled_viewers.md) |
| ARTIQ 输出约束 | [Square 时序](square_timing.md)、[RAM pulse](pulses_and_ram.md)、[RAM 边界](ram_boundary_timing.md)、[virtual Z 与短门](phase_switching_and_virtual_z.md) |
| AOD 与大实验 | [DDS 搬运](transport_dds.md)、[分块](chunked_execution.md)、[磁盘编译与恢复](streamed_execution.md)、[RB](randomized_benchmarking.md) |
| 读出与分析 | [结果与成像](results_and_imaging.md)、[实测与预测对照](capture_comparison.md) |
| 光学制备 | [配置与实验](lab_recipes_experiments.md)、[Yb 制备参考](yb171_full_preparation.md) |
| 开发主库或实验室组件 | [开发指南](development.md)、[公共 API](public_api.md)、[源码分层](source_layout.md)、[设计决定](design_decisions.md)、[CONTRIBUTING](../CONTRIBUTING.md) |

日常按改动行为选择定向测试，并只检查修改的 Python 文件格式。教程迁移需要生成器
与 notebook 同步、全新 kernel 执行；跨阶段整理完成后再建立一次离线集成检查点。
不把完整 CI、全部 ARTIQ 编译和实机验证绑定到每次开发。已完成与未验证范围须分别记录，
不能从“编译成功”推导出光学输出或量子门保真度已验收。
