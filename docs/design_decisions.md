# 当前设计决定与维护约定

本页记录当前协作开发所需的设计约束，随实现变更维护。它不是测试通过记录；历史
本机日志、实验输出及旧计划不随源码分发，也不能代替当前源码和验收证据。
开发流程见[合作者指南](development.md)，具体实现分层见[架构](architecture.md)。

## 实验、配置与参数

- 主链保持 Python 实验 → 源结构 → 逐点排程与物理展开 → 编译产物 → 导出或执行。
  普通 import、构建、编译和报告导出都保持离线；连接与执行必须显式调用。
- 实验函数接收 qubit、命名 pulse、控制/扫描量和准备读出参数，返回 `Experiment`。
  pulse 先命名，正文呈现实验时序；不引入承载整套流程的重型 recipe 配置。
- `LabConfig/DeviceSetup` 管接线、时钟、资源、后端及设备标定；`OpticsConfig` 管共享
  光学工作点；`Qubit.parameters` 管角色标定。循环、执行顺序、读出选择属于代码。
- 新式 qubit 参数可直接修改，Experiment/Section/编译产物保留独立只读快照。
  已保存幅度/时长的 pulse 不追踪 qubit 更新，需显式重建；Store 是可选版本记录。
- 配置文件使用声明的 SI 数字，DSL 使用带单位的量。入口校验值和单位，编译器
  校验时间网格、资源和设备准备窗口，不重复校验已解析的整张参数表。
- `dsl/sections.py` 唯一定义 Section 与组合 API；`compiler/section_schedule.py`
  只放置已有结构。配置不调用 DSL、编译或执行；`lab.py` 保留应用入口。共享 host
  计划读取在 `host_plan.py`，结果与报告无需为读取 shot 顺序导入编译流程。
- RF 是否全局、clock/Rydberg 是否共享由 descriptor 声明。共享输出只发一次波形，
  从接线和登记 qubit 推导实际 targets；显式 targets 是可选断言。不同 UID 不代表
  可单独寻址，搬运到光学区也不把全局 RF 变成局域 RF。

- 可复用实验与操作归 `experiments`；单比特 Clifford 表／recovery 与完整 RB 描述
  是纯数学模块，不反向依赖 DSL。实验构造和 Section 适配单向消费固定序列，目录
  重组不改变 seed、Clifford 顺序或恢复门。专用流程仍由实验室包维护。

## 时序与相位

- 串并行、Section、layer 和 barrier 共用原有源结构。ASAP 与局部 barrier 仍受
  qubit 和共享输出占用限制；独立 frame 不表示资源独立。
- sweep 在 host 构建静态轴；repeat/case 在编译时逐点展开，不是 kernel 实时分支。
  average 保留逐 shot 数据及明确执行顺序，不自动平均波形或相机状态。
- 单一持久 frame F：`shift_phase(delta)` 加 delta，`Rz(theta)` 减 theta。
  `reset_frame` 清整个 F；`reset_phase` 还清继承调制并在最终排程位置重建载波参考。
  两者都不复位量子态；reset_phase 不能打断活动 pulse。
- Spectrum `triggered_sequence` 默认以 sequence 入口定相；`phase_reference="shot"`
  显式使用共同 shot 参考。Section、operation、segment、chunk 不自动复位相位。
  每 shot 从新的 frame 状态开始，不承诺跨 shot 相干。
- 逻辑时间、原生 ARTIQ mu、AWG sample 和 DDS 命令网格分别处理。目标量化必须显式；
  hybrid 不接受 `allow_quantization=True`。不得通过插入 gap、改包络或降采样掩盖
  不可行请求。详细公式和边界见[相位模式](spectrum_phase_modes.md)。

## 后端、native 与搬运

- `artiq` 与 `artiq_spectrum` 是显式后端；不按某条 DSL 指令自动切换。后端保持
  无状态，配置进入编译快照。改变后端语义时维护其版本，内容 hash 不自动扫描源码。
- ARTIQ 的目标展开、源码生成和 mock 验证各归 `backends.artiq` 的独立模块。
  `codegen*` 不依赖验证器；`export` 编排生成、验证与发布。后端命名空间按需加载，
  内部导入使用实际所有者，旧平铺模块保留兼容。
- Spectrum 的样本／命令／bank 归 `backends.artiq_spectrum`，实机执行归 runtime。
  AWG 独立数学期望与有限回放分开；DDS 校验按原意重建并比对完整性，不宣称具有
  独立物理输出模型。`spectrum_calibration` 保留根层共享分析与运行时解析边界。
- 原生组件按 `prefix* → timed* → DSL 控制 → suffix*` 组合，不能门间任意插入。
  源码只读发现、固定与审计，不在 DSL 环境执行用户 ARTIQ 模块。时长契约检查实际
  返回游标，不补等待。共享资源及初始化所有者显式声明。
- `NativeProgram` 的 initialize/finalize 每会话一次、after_shot 每 shot；旧
  `NativeKernelPackage.finalize_method` 仍每 shot。两种生命周期不自动互换。
  新组件把 core reset 留给根初始化，含自行 reset 的旧包不可机械迁入组件 hook。
- 目前搬运使用 Spectrum DDS 驱动交叉 X/Y AOD；A/B 的量子控制继续用 AWG。
  编译内部沿分支更新位置，不修改用户 `q.position`，每 shot 显式回到初始布局。
  DDS 保持独立频率/幅度控制，不建立与 AWG 共用的软件绝对相位账本。
- DDS 到达终点保持非零囚禁输出，shot/换 bank/run 返回后继续保持，关闭会话才停。
  失败停止并使位置状态未知，同会话拒绝继续执行。轨迹与边界见[DDS 搬运](transport_dds.md)。

## 结果、恢复与报告

- 相机触发不自动产生图像或 DSL acquisition；原生读出、采集声明、host 取数和
  数据提交分别配置。空位是有效 0，缺帧不是 0；联合概率必须来自同 shot 数据。
- `RunStore` 区分硬件完成与数据提交，以 run/point/shot/attempt 关联资产。
  unknown 默认停止；恢复只找回原 attempt 数据，不承诺物理 exactly-once。
- `compile_stream` 逐点落盘并限制托管工作区，不是 RSS 上限；输入 AST、全局索引
  和 RB 门描述仍可能增长。完整 store 发布后才可运行，不支持中断编译续编。
- 报告显示实际导出源码及来源哈希。AWG 波形来自最终量化 DAC；DDS 图是命令预测，
  不伪装为 RF DAC 或测量。未知 host/native 时间不编造为固定 shot 间隔。
- mock、真实 ARTIQ 编译、浏览器实显和硬件验收分别报告。编译成功不能证明实际
  冷却、同步、门保真度或相机传输；源码改变后旧 ELF 证据须重新核对或重新生成。

## 协作与兼容

通用能力进主库，实验室私有配置和专用组件独立成包；公共入口见[API 索引](public_api.md)。
教程以实验意图、主要结构和语义为兼容目标，允许更新 import、构造和函数调用；同步
维护生成器，不为旧写法冻结不合理设计，也不借重构改变物理实验。

保留有价值的测试和基线，按行为选择最小充分检查；共享模块变化不自动要求全量回归。
不凭文件名、日期或未跟踪状态删除资料。output、工具链和依赖环境本地保留；需要
分享的证据通过独立产物提供，不把机器日志写成新克隆必须具备的条件。
