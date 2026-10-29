# 实现架构与契约

## 编译边界

1. `dsl` 通过上下文构建 `Node` 树。失败的构建 block 回滚；无硬件调用。
2. `models` 提供不可变 Qubit 快照、JSON 版本存储和校准事务。
3. `compiler` 解出有限 sweep 笛卡尔积，静态选择 `case`，为每点展开实时区块。
4. `hardware` 描述逻辑映射、复合控制配方、协议及设备时序约束。
5. 编译器先保留逻辑区间与 frame，再展开设备操作、资源和准备动作；最终可行性在物理展开后验证。
6. `artiq` 默认按事件结构分组生成参数化 kernel 与 host 参数表；调试模式逐点生成静态 kernel。两者都由独立 mock 模型重建事件进行比较。
7. `visualization` 使用同一个执行计划生成双层时序图；`results/analysis` 管理数据和校准建议。

IR 保存 average/sweep/repeat/case 层次，未展开全部 shots。每个扫描点保存其静态事件，重复 shots 重用同一计划。默认限制 10,000 个扫描点和每点 100,000 个事件，避免无界展开。参数化优化在 exporter 层进行，编译时间表仍可逐点检查。

## 参数化与调试导出

`CompiledExperiment.export_artiq(..., mode='auto')` 是默认行为，适用于模板和自定义 DSL。exporter 使用已验证的物理事件顺序和设备身份识别可复用程序；时间和 DDS 数值成为每点的整数参数。结构兼容的点共享参数化 kernel，不同结构分组。短事件序列可以通过可选事件槽复用其有序超序列：禁用槽不产生 RTIO 事件，保留零 pulse/零 delay 点的编译优化结果。

这不是一个在 kernel 内动态解释任意 Python 对象的执行器。设备调用和事件顺序由生成的程序固定，参数行使用 ARTIQ 可表达的静态类型。没有 host RPC 被新增到严格 pulse 区间。

`mode='unrolled'` 保留逐点 `point_N()`，用于调试和对照。两个模式使用相同 compiled artifact，不重新排程或重新量化。host 平均循环及 `before_shot/after_shot/finish` 接口保持一致。

`verify_equivalence` 对每点分别执行两份生成程序，并核对事件及时间、DDS tuning words、相位参考和 shot 结束位置。host 循环顺序也进行核对。测试覆盖零时长、零延迟、不同平均顺序、自定义多维 sweep、结构变化和参数变异。

单个 shot 内的 `repeat` 仍在编译阶段展开。当前的优化减少扫描点间的程序复制，不承诺减少到固定一个 kernel、不承诺硬件循环执行或真实 ARTIQ 只编译一次。多个无法共享的结构仍会生成多个程序。

## 排程与相位

逻辑事件以 shot 内容起点为零。physical events 加上 `origin_mu` 以容纳先于逻辑起点的 DDS 准备动作。DDS 参数更新寻找合法的前置窗口，不能提前越过该设备前一不同参数 pulse 的结束，也不能侵犯共享更新组的输出或保留资源。

输出的配置和开关按时间顺序提交；阻塞驱动调用区间内不放置其他输出边沿。配置调用全局串行，这是参考后端的保守限制，不是 ARTIQ 的普遍硬件限制。

每个 shot 的 frame 重新开始。DDS 使用 `PHASE_MODE_TRACKING`，参考时间为 `t0 + origin_mu`；`phase` 表示相对于这个 shot 参考的偏移。频率改变时使用同一个参考原点下的新频率，不宣称任意跳频的累积相位自动连续。`delay` 不重置 frame；软件 frame 和 DDS phase accumulator 是不同状态。

相位数值和 DDS tuning words 的量化会记录。设备延迟补偿与相位偏移是独立 calibration，单纯设置一个 latency 不证明光学相位已校准。

`exact_duration` 拒绝不相等长度，用户应自己添加意图明确的 delay。开启时间量化后，先将操作持续时间落到映射设备栅格，再重新排程和验证；不自动改写外部 alignment 约束。

## 协议与结果

首版 `Protocol` 是一个或多个带 offset/duration 的 TTL 触发步骤，可以占用多个逻辑信号。步骤中的 `{qubit}` 替换为目标 ID；读出 duration 为 `None` 时取本实验 Qubit 的 `params.readout.duration`。更复杂的实时制备可以直接在 DSL 中描述；主机加载、相机数据取得和自适应步骤暂由外部 host adapter 负责。

每个 point/shot 中，同一个 handle/qubit 只允许一次测量；重复读出应使用不同 handles。所有扫描点的 handle/qubit 集合必须相同，以保持结果数组结构确定。

`average` 定义采样次数和顺序，不直接平均波形、图像或状态码。`ResultSet.aggregate` 对有效数值归约，返回 mean、valid_counts 和 postselection 策略；无有效值时为 NaN。逐 shot 数据和 sequence 轴始终保留。

## 配置和可复现性

Qubit 在实验构建时冻结，setup/mapping 在编译时复制。编译 provenance 保存源 IR hash、配置内容 hash、所用 Qubit 快照与能力状态。编译对象具有完整性校验，避免修改设备时钟后生成不同 tuning words 却仍使用旧配置摘要。

参数保存是本地文件事务：校验建议来源、单位和值，准备新 Qubit/revision/history，写临时文件并原子替换，成功后才提交内存状态。自动应用必须提供质量策略，和显式应用使用同一路径。未启用自动模式时，拟合只产生建议。

## 真实 ARTIQ 接入前的工作

- 固定真实 ARTIQ、gateware、AD9910/CPLD 版本和 device database。
- 确认 core reference period、DDS sysclk、SPI 参数、更新同步与开关延迟。
- 将参考配置预算替换为驱动契约或经过检查的保守上界，测量必要的设备和光路标定。
- 实现真实制备/读出协议和 host result adapter，验证 arm/trigger/readout 的顺序和结果关联。
- 运行真实 `artiq_compile`；离线 mock 成功不能替代该检查。
- 依次验证 TTL、单 DDS、两 pulse Ramsey、跨通道对齐和 RB，并对照 RTIO analyzer、core log 与示波器/光电信号。

首版不操作仪器，不把 `timing_verified` 标志当作物理证明，也不把真实编译与实机验收标为已通过。

参考：

- [ARTIQ RTIO timeline / errors](https://m-labs.hk/artiq/manual/rtio.html)
- [AD9910 driver implementation](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html)
- [Urukul shared resources](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)
