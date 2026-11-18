# 从配置到实验：教程入口

推荐从 [Ramsey 与 clock_ramsey 的 Plain DSL 版](ramsey_clock_plain.ipynb) 开始。
实验正文直接使用 `play/delay/reset_phase`；熟悉后再读对应的
[qubit_operation 版](ramsey_clock_walkthrough.ipynb)，学习如何复用短小的门操作。
两版使用相同设备、参数、准备／读出与扫描含义，实验正文都保持可见。

## 一条上手路线

**device db → descriptor → LabConfig → Qubit → native ARTIQ →
experiment definition → compile & visualization → connect to real hardware**。

- 先展示可编辑的 ARTIQ device db 和 Spectrum spec db，再展示完整接线 descriptor。
- `LabConfig` 集中保存 setup、qubit 与执行模式；`Qubit` 保存角色参数，pulse 在实验外先命名。
- native 登记读取既有源码，不在普通 Python kernel 中 import 或执行 ARTIQ 类。
- 实验函数显式接收 qubit、pulse、扫描量和准备／读出，返回 `Experiment`；编译放在函数外。
- 先看时序与最终波形，最后才介绍真实连接；默认 Run All 始终离线。

实验作者继续使用 `from taiyidsl import ...`。内部包重组不要求改为导入编译器或运行器
私有文件。正文用直接的字典、物理量与短函数，不把 Python 配置框架或大量 helper
作为入门前置。数据分析可用 NumPy/Matplotlib；数值辅助不能隐藏实际 pulse 时序。

## 安装和打开

从源码 checkout 根目录运行：

```powershell
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/ramsey_clock_plain.ipynb
```

使用项目 uv 管理的 CPython 3.12，选择 Restart Kernel → Run All。
安装 wheel 只提供库，不包含教程所需的 examples 和 native 源码输入。
默认不连接设备或执行 ARTIQ kernel。所有示例参数未经实验室校准；GreenMOT fixture
只演示 TTL 边界，不制备原子。相机 TTL 不自动取得图像。

## 选择课程

| 教程 | 适合何时阅读 | 本地输出 |
| --- | --- | --- |
| [Plain Ramsey／clock_ramsey](ramsey_clock_plain.ipynb) | 第一个完整实验，直接阅读 pulse 顺序 | `output/plain_ramsey/` |
| [qubit_operation Ramsey／clock_ramsey](ramsey_clock_walkthrough.ipynb) | 同一实验中复用 RF π/2、clock 2π 操作 | `output/simple_ramsey/` |
| [¹⁷¹Yb global-RF tune-up](yb171_global_rf_tuneup.ipynb) | RF、clock、Rydberg 与双比特校准的实验学习路径 | `output/yb171_tuneup/` |
| [Clock Ramsey＋IZ 搬运](ramsey_clock_transport.ipynb) | 在已知实验中加入 AOD 轨迹和 AWG/DDS 同步 | `output/ramsey_clock_transport/` |

重复运行会更新各自的演示输出目录；这些目录不能用来保存真实测量数据。
源码 notebook 不带执行输出，有用的执行副本和报告保留在本地。

### Ramsey 两版共同的实验含义

只定义并观察 q0；RF 仍声明为公共场，不引入本课不观察的其它原子。
Ramsey 扫描暗时间 `tau`；clock Ramsey 扫描第二 RF 的 `phase`，其余控制参数为标量。
RF 频率在函数开头显式计算 `q0.parameters.rf.frequency * Hz + detuning`，
两次 pulse 共用，不再把同一失谐重复加到相位。

命名 `pi2_pulse` 保存 Qubit 的半 π 时长和幅度，前后 RF 门复用同一个对象。
修改幅度／时长后重建 pulse；频率没有保存在 pulse 中时，频率修改无需重建 pulse。
随后都要重新调用实验函数、编译并刷新报告。旧实验与编译产物保留各自参数快照。
`QubitStore` 是可选版本记录接口，不是入门前置。

clock Ramsey 的主体为 RF π/2 100 μs → clock 2π 200 μs → RF π/2 100 μs，
中间没有隐藏等待；B 卡保留共同时间轴上的起始空白。示例 RF 为 30＋1＝31 kHz，
clock 的 200 kHz 是载波频率，不是 Rabi 频率。这些值只解释编译行为，不证明门已标定。

两版还展示可选 `@experiment` 写法及其与普通函数的波形等价断言。
这些单个 notebook 内的断言与 plain／operation 两版之间的对照是不同检查。
查看器展示最终 DAC 样本；未知 host/native 时间不计为固定的墙钟等待。

### Tune-up 与搬运扩展

Yb tune-up 在各实验前说明原理、扫描与分析方法、适用边界和参考出处。
RF 部分包括二维 Rabi chevron、幅度×时长、二维 Ramsey、XY8、spin lock、
面积误差放大、人口保持与 RB；clock 部分包括二维 chevron/Ramsey、等时 shelving
相位/保持对照与差分 light shift；Rydberg 部分包括 interacting-pair chevron 与返回诊断。
CZ 部分覆盖二维 closure/echo、Bell／宇称、输入态相关损失、相干重复、
随机回波对照、无回波 SSB 及共同相位扫描，并给出平滑波形候选、训练/留出词与
幅度/失谐鲁棒性扫描。小网格用于离线学习，实际测量需替换准备与分态读出。
每个完整门包含 clock 上下行；SSB 不宣称完整四维 Clifford RB。
数学核验、合成数据与真实测量分别标注。原论文与具体实验的对应见
[benchmarking 与波形优化参考](yb171_benchmarking_references.md)。
数值分析辅助位于 [yb171_tuneup_analysis.py](../../examples/yb171_tuneup_analysis.py)。

搬运版保留 RF π/2 → move 到 IZ → clock 2π → move 回初始位置 → RF π/2 的正文。
A/B 使用 AWG，C 使用 Spectrum DDS20，同组触发并按声明延迟对齐。
通用查看器显示最终 AWG DAC 与 DDS 分段频率／幅度；位置是编译计划，不是实测轨迹。
`q0.position` 保留初始位置，C 到终点后维持非零 hold，关闭连接会话时停止。

## 准备、读出和真实连接

本组教程沿用 `NativeKernelPackage.from_package(...)` 登记 GreenMOT fixture。
它的初始化含 `core.reset()`，`finalize_method` 每 shot 执行。新建独立 MOT、冷却或
相机模块使用 `NativeModule.from_package(...)`＋`NativeProgram`；其初始化／清理
按会话运行，不能仅换 API 名称就迁移旧包。扩展时先读[原生组件指南](../native_components.md)
和[最小实验室包](../../examples/lab_extension/README.md)。

真实连接是教程末尾的独立步骤：提供真实设备库、接线、校准、ARTIQ 解释器及仍有效的
clock session 标识，再显式调用 `lab.connect()` 和 `session.run()`。
连接检查与执行上传是不同动作；上下文结束会释放会话拥有的资源。
这些操作不进入离线 Run All 验收。

执行 receipt 不含相机帧。`HardwareSession.run()` 当前没有 detector adapter 注入入口；
相机扩展另见[读出契约](../readout_adapters.md)。不要把缺帧当空位，也不要为了取回
丢失的数据自动重做已完成的硬件 shot。

## 维护生成器与离线验收

教学源码入口：

| Notebook | 生成器 |
| --- | --- |
| Ramsey 两版 | [build_ramsey_tutorial.py](../../scripts/build_ramsey_tutorial.py) |
| Yb tune-up | [build_yb171_tuneup_notebook.py](../../scripts/build_yb171_tuneup_notebook.py) |
| Clock transport | [build_clock_transport_tutorial.py](../../scripts/build_clock_transport_tutorial.py) |

修改课程时同步生成器、其输入与 notebook。Ramsey 生成器把实验室声明完整展开到
可编辑 cell；transport 生成器复用 plain notebook 的配置，所以重建时先运行 Ramsey：

```powershell
uv run --locked --group notebooks python scripts/build_ramsey_tutorial.py
uv run --locked --group notebooks python scripts/build_yb171_tuneup_notebook.py
uv run --locked --group notebooks python scripts/build_clock_transport_tutorial.py
```

离线执行使用 [execute_hybrid_tutorial.py](../../scripts/execute_hybrid_tutorial.py)，
每份 notebook 启动独立的项目 Python kernel，按序执行，不注册全局 kernelspec。
它需要本机 Jupyter 的 localhost 通信权限。按影响选择单份课程；跨阶段验收覆盖四份：

```powershell
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/ramsey_clock_plain.ipynb --output output/plain_ramsey/tutorial --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/ramsey_clock_walkthrough.ipynb --output output/simple_ramsey/tutorial --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/yb171_global_rf_tuneup.ipynb --output output/yb171_tuneup/execution --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/ramsey_clock_transport.ipynb --output output/ramsey_clock_transport/execution --semantics
```

执行成功后，所选输出目录包含 `.executed.ipynb`、静态 HTML 阅读副本和
`notebook_verification.json`（源码 hash、kernel 与执行结果）。实验报告保存在各课程
自己的报告目录。`--semantics` 额外保存 `tutorial_semantics.json`；核对代码在同一 kernel
执行，保留时序、相位、采集和最终程序，不加入发布 notebook。命令与既有文件不是
本次检查通过的证明，应读取对应源码的实际记录。

维护者可用 [语义比较脚本](../../scripts/tutorial_semantics.py) 核对两版，无需再执行 notebook：

```powershell
uv run --locked python scripts/tutorial_semantics.py output/plain_ramsey/tutorial/tutorial_semantics.json output/simple_ramsey/tutorial/tutorial_semantics.json --pair compiled=compiled --pair clock_compiled=clock_compiled
```

CI 同时执行四份课程，比较两种 Ramsey 写法，并检查各版普通函数与 `@experiment` 的等价性。

跨阶段检查还要直接比较 plain／operation 对应实验的事件、扫描顺序、相位、最终
DAC/DDS 与采集索引；不能仅检查两份 notebook 各自不报错。改变源码行号可能影响
provenance，不应据此认定物理实验改变，也不应盲目覆盖语义基线。
定向检查与完整离线 CI 的关系见[贡献指南](../../CONTRIBUTING.md)。

## 按需继续

[综合进阶 notebook](hybrid_from_scratch.ipynb) 保留多原生组件、双比特 RB、自动分块、
磁盘编译、联合读出和持久记录。演示 CZ 未校准，合成 RB 结果不是原子动力学仿真。
其生成器为 [build_hybrid_tutorial.py](../../scripts/build_hybrid_tutorial.py)。
旧 [ramsey_from_scratch.ipynb](ramsey_from_scratch.ipynb) 作为历史参考保留。

配套入口：[开发指南](../development.md)、[原生组件示例](native_components_walkthrough.md)、
[剩余验收](remaining_work_and_priorities.md)。真实 ARTIQ 编译步骤见[工具链说明](../artiq_toolchain.md)。
Notebook 执行、真实 ARTIQ 编译、浏览器交互和仪器验收分别记录；默认教程验收只离线执行。
