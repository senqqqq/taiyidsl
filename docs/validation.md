# 验证记录

## 协作版分层与教程验收（2026-11-17）

本次记录对应四阶段重构完成后的本地 Windows 检查。下面的早期记录保留为历史证据，
不代表当前源码已再次通过真实 ARTIQ 编译或实机验收。

- 完整离线 pytest 首轮 1493 通过、35 失败。失败集中在已过时的 Spectrum fixture、
  future-schema 示例、内部 monkeypatch 路径、报告源码位置及展示断言。核对实际契约
  和修改前源码后修复；35 个失败用例全部定向复验通过，另检查相关观测器确实生效。
  未再次重复完整套件；新增轻量后端导入边界检查单独通过。
- plain Ramsey、qubit_operation Ramsey、Yb tune-up、clock transport 四份 notebook
  在最终源码上分别以全新 kernel 完整离线执行。30 个具名编译对象与第四阶段前相同；
  两种 Ramsey 写法和各自的普通函数／装饰器写法也相同。忽略的仅是来源位置、逻辑
  节点 ID 和 operation_path；保留配置、时间、相位、DAC/DDS、native 与执行顺序。
- 三类 ARTIQ 代表案例的 auto／unrolled 生成源码、独立 mock 轨迹与检查报告不变。
  AWG 跨 tile 调制的 packed 字节和 DDS 闭合搬运命令／末端保持不变。DDS 检查仍是
  原意重建及完整性验证，不是独立的硬件物理模型。
- 新 wheel 在独立安装目录中检查 135 个公开符号、203 个源码／资源文件和实际编译，
  确认未从源码 checkout 补载缺失模块。依赖复用项目 uv 锁定环境，安装使用 no-deps；
  这不等于从空机器联网安装全部工具链。
- 修复 HardwareSession 对采集计划的旧方法名调用；使用真实编译对象与模拟通信验证
  正常执行记录和缺少 detector adapter 时的提前拒绝，不启动设备 worker。

本地完整记录：`output/refactor/phase4/verification_summary.json`，首轮及定向复验
JUnit 记录均保留。发布 notebook 不含执行输出，执行副本与报告保存在忽略目录。
CI 配置覆盖 Windows/Linux 完整离线检查及四份教程；本次未触发远端 CI，未操作仪器，
未做浏览器视觉验收。生成 kernel 源码不变的代表调用链复用编译边界证据，本轮未重编
真实 ARTIQ。实验室仍需验证同步、光学时序、相机取数、恢复和物理校准。

## 多卡、实验组合、原生组件与大规模 RB（2026-11-12）

本条记录本轮新增修订，下面保留各历史版本的结果，不混用测试数和相位语义。

- 最终全量 **1376 项通过**：`output/extended_hybrid/final_regression.xml`。
  本轮修改的 Python 文件 Ruff lint/format 通过；未执行 wheel/sdist 或实机操作。
- 真实 ARTIQ 9.9664 / rv32g 离线编译 **25 个 kernel 通过**，覆盖多组件、union 初始化、
  命名 clock frame 的 Square/RAM/POWASF、完整 RB 活动点和 2/3/4 TTL。
  验证源码与 ELF hash，所有设备库均为 disconnected compile-only fixture。
- 新增边界包含旧参数与 unused mapping 兼容、layer/局部 barrier、clock phase/reset、
  自动容量分块、受控工作缓冲、唯一 hash 样本、跨块 native 身份/旧块回收、journal 中断
  与找回、完整 Clifford 群和矩阵 recovery、配对分析、最终 DAC/捕获一致性。
- 流式 RB host mock 保存 32 次观测，已提交项恢复不重复 dispatch。人工数据与 mock
  journal 均有明确来源，不是实验数据，也未执行生成的 ARTIQ kernel。
- 交付复核脚本：`scripts/verify_extended_hybrid_delivery.py`；
  统一证据：`output/extended_hybrid/verification_summary.json`。
- 浏览器实际查看未完成：桌面 app 授权超时，内置浏览器禁止本地 file:// URL。
  HTML 链接、JS 解码和 packed DAC 对等检查不替代浏览器视觉/交互验收。
  同步偏斜、模拟幅相、RTIO 吞吐、真实采集恢复和 CZ 门保真度仍需硬件验收。

## 800/200ns相位能力与virtual Z（2026-11-06）

- **559项全量测试通过**，Ruff和离线wheel/sdist通过；JUnit在
  `output/short_gate_timing/test-results.xml`。
- 40项短门能力矩阵、11份成功导出，明确保存失败原因：单板RAM800ns换相在声明模型下
  通过；Square800/200ns、RAM200ns及双板RAM800ns换相仍拒绝。反事实Lp=La只用于
  隔离吞吐限制，不导出为可运行实验。
- `Rz/ rz`零时长逻辑旋转已实现；22项新测试验证任意角SU(2)等价、frame与测量符号、
  初始/末尾操作、800ns后续真实硬件相位、200ns带宽拒绝、非驱动信号不受影响。
- 1024×800ns RAM、8×800ns展开、800ns RAM Rz序列、200ns RAM同相位对照、
  双板200ns Square同相位对照，共5份真实驱动轨迹、**10个ARTIQ9.9664/rv32g
  kernel编译通过**。同相位对照不能当成200ns任意换相支持。
- 新驱动路径审计保存版本及SHA256：POW416ns，完整profile1248ns，CPLD profile
  选择Rev8为416ns、Rev9为896ns。所有数字都是离线驱动游标，不是模拟输出延迟。
- 当前POW+autoclear单一响应假设未获实机证明，零尾方案不是通用解法；未执行
  硬件kernel。完整限制及解决方向见 [相位切换与virtual Z](phase_switching_and_virtual_z.md)。

## RAM 门边界相位与连续重放（2026-11-05）

- backend 7：显式双管线延迟，POW 预写、定时 IO_UPDATE 和独立输出响应；
  相位保护检查实际量化零值尾部，不插 gap 或改写波形。能力与限制见
  [RAM 边界时序](ram_boundary_timing.md)。
- **492 项全量测试通过**，包括30项新编译/时序测试与16项新导出/驱动测试。
  覆盖83 MHz追踪误差、非零尾拒绝、frame-only重启侵入下一门、reservation、
  IO_UPDATE低电平、双板同步边沿和漏写/漏提交的变异。Ruff和离线wheel/sdist通过。
- 1024门auto：401 RAM字，总长8.192 ms；8门unrolled和双独立板8门+末尾reset
  均通过mock。实际驱动Python审计验证416mu POW游标、驻留数据和数字边沿。
- 三份导出共 **6 个 ARTIQ 9.9664+071b41b / rv32g kernel 编译通过**（含初始化），
  ELF/源码哈希与报告位于 `output/artiq_compile/ram_boundary_*`。
  汇总与JUnit位于 `output/ram_boundary_sequence/`。
- **未执行硬件 kernel**。112/80mu为未测量示例；真实DAC/RF/IQ、板间时钟、
  CPU/FIFO吞吐与光学路径均未验收。并非任意包络零间隔能力；不满足原有零尾保护
  的不同相位门明确拒绝。真实驱动审计脚本当前只覆盖同profile驻留路径。

## Square 连续更新与硬件相位命令（2026-11-05）

- 最终446项全量测试通过；Ruff检查/格式及离线wheel/sdist构建通过。
  测试记录：`output/square_timing_validation/test-results.xml`、`pytest.txt`。
- backend 6 区分缓冲准备、IO_UPDATE提交和matched输出生效；连续变参不关开RF、不加gap。
- 新增实时`shift_phase/reset_frame`、共享组所有参与通道重准备、独立组同步、不同延迟、
  无后续play及无play时硬件frame、同刻命令、资源保留和RTIO容量边界的测试。
- 旧Square的1000mu预算错误已由真实驱动复现；默认改为3000，真实游标2664mu。
  新profile写入真实1248mu，示例预算1280。compiler/target/mock均校验声明，不用假预算自证。
- `scripts/verify_square_driver.py` 用真实安装的ARTIQ驱动函数体和虚拟RTIO验证寄存器字、
  数字边沿、模式、SPI游标和coarse事件计数，不连接设备。
- `output/square_timing_validation/verification_summary.json`：5种场景×2种模式的10份轨迹、
  20次ARTIQ9.9664+071b41b/rv32g编译全部通过；共享板Rev8轨迹亦通过。当前生成源码、
  manifest、轨迹、编译报告与ELF哈希已核对。
- 冻结的逻辑时序、采集与host顺序继续验证；旧版本物理事件格式的变化单独验证，
  不通过重写历史指纹来掩盖输出变化。
- 输出延迟80mu仍是示例假设，16位POW补偿存在量化；RF/IQ时序、时钟相干、CPU提交吞吐、
  FIFO余量及core日志仍需实验室验收。没有真实kernel或仪器执行。

## 并行调度修复、窗口索引与模板更新（2026-11-03）

- `ImageResults` 逐点验证图像 handle 对应唯一逻辑采集事件，防止独立触发混为一帧。
  共享相机多 site 与标量 `ResultSet` 的 per-qubit 索引保留。
- ARTIQ backend 5 使用区间与边沿索引；原顺序放置失败后，对相同输出 deadline
  按较晚 release 优先重试一次。覆盖设备映射置换、分支倒序、真正不足的窗口、
  嵌套共享输出、显式保留资源、独立 TTL 边沿。保持输出时刻，不自动加 gap。
  该策略仍是有限启发式，不保证解决所有可行排程。
- Rabi/Ramsey/RB 改用 pulse 模板与 `play` 单次覆盖。RB 按长度分组 case，
  8 变体共 30 点与修改前冻结的逻辑、物理、mock、初始化和采集指纹一致。
- 本机 CPython 3.12 基准：32 qubit × 32 变参门 5.900→0.339 秒；
  8 qubit × 256 变参门 6.090→0.710 秒。保持 11 案例的事件数、源码字节数与 origin。
  常参数 8×256 案例为 0.592→0.716 秒，不能宣称普遍加速；计时非硬件执行速率。
  数据位于 `output/parallel_audit/benchmark_{before,after}_preparation.json`。
- 全量 **405 passed**，Ruff lint/format 与离线 wheel/sdist 构建通过。
  JUnit：`output/parallel_update/tests.xml`。
- ARTIQ 9.9664+071b41b / rv32g：三模板 15 个 kernel/参数行真实编译通过，
  backend 5 重新导出源码与设备契约相同后复验 ELF；精确偏移、parallel Rabi、
  修复后的准备窗口案例另有 8 个新编译通过。使用测试 device_db，未执行 kernel。
  证据：`output/parallel_update/artiq_verification.json`，明确区分复用与新编译结果。
- 默认串行/显式并行语义不变；RAM 同步提交限制不变。RTIO、相机、跨板及原子处
  光场时序未验收。新示例和写法见[实验书写指南](experiment_authoring.md)。

## 保持 RAM 开启的快速重放（2026-11-02）

本节记录快速 RAM 重放功能的历史验证范围。

- 同profile/FTW的相位变更仅写POW；实际tracking POW相同则仅IO_UPDATE。
  两者保持RAM开启，profile或频率变化仍走原完整rearm。
- 真实ARTIQ9.9664+071b41b / rv32g：1024门XY交替和相同门重复的事件表版本、
  各8门unrolled版本，初始化与shot共8个kernel全部生成ELF。
  报告：`output/artiq_compile/ram_fast_{xy,repeat}{,_unrolled}/compile_report.json`。
  使用host=None测试device_db，未执行kernel、未连接硬件。
- 真实驱动方法体游标审计：POW写入52*C，coarse8mu时416mu，Rev8/9均通过。
  报告：`output/ram_fast_replay/driver_timing_rev9.json`及`driver_timing_rev8.json`。
- 假设输出延迟112mu：相位预算480mu时gap592ns，416mu无余量预算时528ns；
  无SPI重触发gap112ns。不是已实测的CPU/RTIO/光学极限。
- 独立mock比较实际POW、驻留样本、触发与RF边沿；验证漏写相位、SPI预算过短、
  资源冲突、相同逻辑相位但tracking POW变化、快速路径与完整rearm混合和跨扫描点。
- 全量 **376 passed**；Ruff lint/format、wheel/sdist构建通过，发行包排除工具链和缓存。
  JUnit：`output/ram_fast_replay/test-results.xml`；汇总与源码/ELF哈希核对记录：
  `output/ram_fast_replay/verification_summary.json`。

入口：[快速重放例子](../examples/ram_fast_replay.py)、[RAM指南](pulses_and_ram.md)。

## 驻留 RAM 波形库与长门序列（2026-10-31）

本节记录最初完整rearm路径的验证。

- 同shot 1024次X/Y/X2/Y2标签播放，只预加载两个401字幅度包络，合计802 RAM字。
  每次相位写POW，幅度变体选择实际量化后的波形；无门间RAM写入。
- 默认事件表kernel与调试unrolled的独立mock事件等价，检查完整门长、profile、
  幅度样本、phase tracking、重装配窗口、frame/路径延迟与资源冲突。
- 全量 **350 passed**，随后新增SPI预算边界测试 **1 passed**；Ruff通过。
  报告：`output/ram_replay/test-results.xml`、`spi-boundary-test.xml`。
- 真实ARTIQ9.9664+071b41b / rv32g：1024门事件表及8门调试展开，各自初始化和shot
  kernel均编译为ELF；使用测试device_db，未执行kernel或连接硬件。
  报告：`output/artiq_compile/ram_gate_sequence/compile_report.json` 和
  `output/artiq_compile/ram_gate_sequence_unrolled/compile_report.json`。
- 独立驱动方法体游标审计：Rev9为3440mu、Rev8为2960mu（coarse=8mu）；报告在
  `output/ram_replay/driver_timing_rev9.json` / `driver_timing_rev8.json`。
- 声明预算4000mu、matched输出延迟112mu时要求gap>=4112mu；无准备余量模型下限
  为3552mu。两者都不是实测的无RTIO underflow下限；超预算或资源冲突明确拒绝。
- 旧单包络保持原模式；配置rearm预算后，单门/多门扫描点均保持matched模式。
  错误profile、相位、latency模式、漏掉参数提交、门间写RAM的变异均被拒绝。

例子：[ram_gate_sequence.py](../examples/ram_gate_sequence.py)；接口和硬件边界见
[RAM指南](pulses_and_ram.md)。驻留波形数量受8profiles/1024字限制，core事件表内存
仍随门数增长；同更新组独占与同shot不混用Square/RAM的限制仍保留。

## 完整 ARTIQ 工具链与真实离线编译（2026-10-30）

本节记录初次安装验证；下方的“未安装/未真实编译”描述保留当时的历史验证范围。

- 官方 MSYS2 安装包 build202416 的大小和 SHA-256 均核对通过，安装器返回0。
  ARTIQ9.9664+071b41b、Python3.14.5、llvmlite0.47.0、LLD22.1.4。
- `artiq_compile` 最小 kernel 生成真实 ELF；初始化和全部49个 Rabi参数行、初始化和
  全部4个 FlatTopGaussian扫描点，共55个 kernel/参数行通过真实 Core.compile。
- 使用显式 `rv32g` 目标与自动生成的测试 device_db（host=None，真实驱动，虚构通道），
  未连接仪器，未执行 kernel。尚未验证其他CPU目标或真实实验室设备数据库。
- 故意把字符串传给 `delay_mu` 的负例被真实编译器拒绝，入口返回1并保存类型错误报告。
- 报告与ELF：`output/artiq_compile/rabi_auto/`、`output/artiq_compile/offline_imaging/`；
  汇总：`output/artiq_compile/verification_summary.json`。报告保存版本、源码/ELF哈希与目标。
- 官方版本的 `CommKernelDummy` 缺少close，清理阶段有警告，成功例仍返回0；未修改驱动。
- 项目 **320 passed**，Ruff lint/format通过，wheel/sdist构建通过；发行包包含离线检查
  入口和PowerShell启动脚本，不包含工具链二进制。JUnit：`output/artiq-toolchain-tests.xml`。
- RAM多门重放仍未实现；用户已确定后续采用少量驻留波形反复调用，不拼接整个长门序列。

安装和复现命令见 [ARTIQ工具链指南](artiq_toolchain.md)。RTIO吞吐、SPI实际预算、板卡同步、
RF/光学输出和门保真度仍须实机验收。导出默认manifest不自动声明真实编译已通过，
应读取与源码哈希匹配的独立编译报告。

## 参数化包络与相机结果链验收（2026-10-30）

本节保留该阶段的历史验证结果。

- 完整测试 **308 passed**；Ruff lint/format通过，locked依赖同步通过。
- 新增检查覆盖play单次覆盖、flat_duration扫描、RAM采样/容量/启动延迟、共享资源保留、驱动参数和RF gate完整时长、Square/RAM状态恢复。
- 采集检查覆盖一帧多个site、重复handle冲突、相邻新触发必须有低电平间隔；原有Square和光学示例的事件基线继续通过。
- 结果检查覆盖五类完整快照、旧schema读取、HDF5往返与失败写入保留原文件、原始图像与ROI判别一致性、缺帧及存活率分母。
- `examples/offline_imaging_workflow.py` 和 `examples/yb171_preparation_reference.py` 已运行，生成存档、图表、参考流程和离线编译产物。
- 报告：`output/parametric-results-tests.xml`。wheel和sdist离线构建通过。
- 本机没有ARTIQ模块，真实ARTIQ编译和硬件/光学验收均未验证。示例RAM延迟64mu不代表实测标定。完整MOT、磁场/阱ramp和真实相机SDK尚需实验室适配。

## 轻量 OpticsConfig 重构验收（2026-10-30）

本节为较早的重构验收结果。

- 完整离线测试 **240 项通过**；项目根目录 Ruff lint 和 format 检查通过。
- 新基线覆盖初始化、RSC 轮数扫描、x 轴边带谱的 cyclic 与 sequential 两种平均模式，
  共 **60 个扫描点**。全部逻辑事件（包括零时长 delay）、物理事件、独立 mock 驱动事件、
  DDS 数值与相位参考、shot 结束时间、host 顺序、采集索引和设备初始化计划逐点一致。
- 上轮重构前的 **30 点基线**继续通过，保留跨两轮变更的行为证据。
- 初始化仍使用触发式读出，RSC 仍使用显式照明读出。默认三个扫描分别为 12/6/12 点，
  各共用一个参数化 shot kernel，参数化与展开导出均通过独立 mock 等价检查。
- 参数模型改为 schema 2 的 `OpticsConfig`；普通映射也可传入 Experiment。
  快照不依赖光学模型，编译产物保存默认参数，操作 IR 保存单次调用的生效参数。
- 测试覆盖单次覆盖后的默认值恢复、零值覆盖、单位与静态 sweep、未知字段、非法范围、
  深层只读快照、JSON 往返，以及手动构造的 NaN/Inf/可变值单位量。

冻结基线：`tests/fixtures/pre_optics_refactor_events.json`，包括原始源码和配置摘要。
逐点比较：`output/optics_refactor_equivalence.json`；完整 JUnit：`output/test-results.xml`。
示例与产物入口见 [参数、操作和实验流程指南](lab_recipes_experiments.md)。

旧 `RecipeSet`、嵌套 recipe 类和 schema 1 光学参数文件已移除；新参数位于
`examples/config/optics/`。执行顺序、循环次数、读出选择位于 Python 操作函数及实验参数，
迁移需要调整字段结构，不能仅替换 schema 版本号。LabConfig 与设备初始化策略保持原样。

**真实 ARTIQ 编译及实机行为仍未验证。** 所有检查均为离线语义、导出及模型一致性检查。

日期：2026-10-29。环境：项目 uv 环境、CPython 3.12.15。没有连接或操作仪器。

## lab / recipe 统一重构验收（2026-10-30）

本节是本次重构的验收记录；下文较早阶段的测试数量与源码行数为历史记录。

- 全套离线测试 **190 项通过**；Ruff lint / format 检查通过。
- 初始化 12 点、RSC 轮数扫描 6 点、x 轴边带谱 12 点，共 **30 个扫描点**，
  与重构前冻结基线逐点一致。比较包括逻辑事件、物理事件、DDS 寄存器与相位参考、
  事件顺序、shot 结束位置、扫描坐标和 host 执行顺序。
- 原初始化的触发式读出与原 RSC 的照明读出分别保留，未替换为同一种序列。
- 三组新示例均通过参数化／展开两种导出的独立 mock 等价检查，默认各共用一个 shot kernel。
- 新增 LabConfig JSON 加载、独立 RecipeSet、不可变实验参数快照和已用设备选择测试。
  初始化动作单独检查；注入多余开关、DDS 初始化或寄存器写入均会被拒绝。
- 早期 backend 提取的六组 event/order 哈希保持原值。生成源码哈希因本次有意改变设备选择、
  初始化及资源声明而更新；更新前已检查事件不变及两种导出的 mock 等价性。

冻结基线：`tests/fixtures/pre_refactor_events.json`；本次逐点报告：
`output/refactor_equivalence.json`；完整 JUnit：`output/test-results.xml`。
示例输出位于 `output/state_initialization/`、`output/rsc_round_scan/` 和 `output/rsc_thermometry_x/`。
构建与使用说明见 [lab / recipe / experiment 指南](lab_recipes_experiments.md)。

设备初始化范围是本次**有意改变**的部分，不纳入“初始化也完全不变”的承诺。
exporter 只请求实际使用的输出；与未用通道共享资源的 DDS 不自动 init，
而要求显式满足 manifest 中的外部准备与资源所有权契约。profile 必须完整声明可能受共享
总线、初始化与 IO_UPDATE 影响的通道；core reset 仍需整个 RTIO 域独占。

**真实 ARTIQ 编译、实机初始化影响、RTIO 与光学时序均未验证。**
mock 检查只证明模型内的一致性，不证明硬件隔离。以下历史验收结论不扩大这一范围。

---

## 后端边界与实验室配置重构

2026-10-29：提取 `Backend` / `BackendCapabilities` 接口，将 ARTIQ 的物理展开、
设备契约和相位策略移入 `backends/artiq.py`；共享排程、事件容器与序列化独立。
`LabConfig` 集中管理 backend、setup、mapping、protocol 和 calibration。

- **129 项 pytest 测试通过**，无跳过；本次报告为
  `output/backend_refactor/test-results.xml`。
- 重构前记录的 Rabi、Ramsey、RB 行为指纹与重构后完全一致，覆盖 cyclic / sequential
  两种平均顺序、逻辑与物理事件、时间、参数，以及 auto / unrolled 两种生成源码。
  固定基线在 `tests/data/artiq_reference.json`，不能用自动更新基线掩盖行为变化。
- 测试专用的第二个 backend 使用同一 DSL、新设备 kind、自己的时钟网格与物理事件，
  能编译和导出，不继承 ARTIQ 的 DDS 配置窗口限制。错误地使用 ARTIQ exporter 时会拒绝。
  另覆盖能力拒绝、注册、后端快照、旧接口、Square 子类和前端导入独立性。
- 两个实验室 profile 保留原有通道映射与协议；初始化示例在隔离目录执行成功。
  完整合成 tune-up 工作流通过，结果在 `output/backend_refactor/tuneup_workflow/`。
- 根目录 Ruff lint / format 检查通过；wheel 和 sdist 构建成功，确认 wheel 包含全部
  新后端及配置模块且不含环境或缓存文件。直接从 wheel 导入，完成 profile、编译及
  通用导出的冒烟检查。构建产物在 `output/backend_refactor/dist/`。

本次使用工作约定的 CPython 3.12 运行时，并从项目 uv 环境加载已锁定的测试依赖。
目标相关的 DDS 时钟/配置栅格校验现在发生在 ARTIQ 编译阶段；通用 Device 允许新 kind，
并不意味着已有相应硬件支持。后端身份、版本与能力新增到 provenance 和内容摘要，
因此摘要变化不代表脉冲或生成程序变化。

真实 ARTIQ 编译、RTIO/光学时序和硬件验收仍为 `not_verified`。
第二个 backend 仅为接口测试，不是新增的硬件驱动，也没有新增部署或执行仪器的入口。

## 自动化结果

- **113 项 pytest 测试通过**，无跳过；JUnit 报告：`output/test-results.xml`。
- 根目录 `ruff check .` 与 `ruff format --check .` 通过：88 列，完整启用 `E/F/W/I/UP`。
- Rabi → 校准建议 → 显式持久化应用 → 新参数 Ramsey/RB 的完整示例通过。
- 三个实验均生成了 compiled JSON、独立离线 HTML、ARTIQ 源码/manifest 及结果文件。
- 导出源码通过 Python 语法和独立 fake-driver 事件检查；变异测试能捕获时序、寄存器、目标时钟和配置预算错误。
- 参数化和展开导出逐点等价检查通过，包括实际驱动事件顺序、时间、DDS 寄存器值与相位参考、配置耗时和 shot 结束位置；生成的 host 循环及读出适配器调用顺序也通过检查。
- 离线 HTML 的内嵌脚本通过 Node.js `--check` 语法检查；没有外部 script 加载依赖。
- 初始化示例新增 3 项测试：零抽运点、对照与 push-out 等长读出时序、配置快照，以及隔离运行示例并生成完整离线产物。

## 本地规范整改与行为保持

固定 CPython 3.12.15，保留依赖锁文件；英文 README、中文指南、贡献指南、PR 模板和跨平台换行配置已补齐。源码包明确限制打包范围，不含环境、缓存、运行数据或临时目录。

整改前保存了源码语法树和四组实验的执行事件报告。原有 22 个 Python 文件中，19 个在格式调整后语法树完全一致；其余 3 个仅有 Ruff 的标准库现代化调整：`typing.Mapping` 改为 `collections.abc.Mapping`，`timezone.utc` 改为等价的 `datetime.UTC`。实验控制逻辑没有改写。

重新生成 Rabi、Ramsey、RB 和 initialization 示例后，共 123 个扫描点的事件轨迹和结束时间与整改前一致；32,400 次 host 调度的顺序检查同样保持一致。比较报告：`output/standards_review/behavior_comparison.json`。源码行号及包含来源位置的 provenance hash 会随格式变化，不能把这些元数据差异当成时序变化。

wheel 和 sdist 构建通过，并检查了内容清单；在新的隔离环境中按锁文件安装运行依赖及 wheel，确认包来自 `site-packages`，完成 Rabi 编译与双模式导出等价性检查。记录：`output/standards_review/wheel_smoke.json`。

`.github/workflows/ci.yml` 已配置 Windows/Linux、固定 Python、Node 22、锁文件同步、根目录 lint/format、pytest、构建和示例产物留存；YAML 本地解析通过。**尚未上传仓库，因此远程 GitHub Actions 和 Linux runner 执行未验证。** 本次没有创建远程仓库、提交或上传代码。

示例合成数据的 Rabi π 时长真值为 12 µs，拟合约 12.014 µs。Qubit revision 从 0 更新到 1，旧实验仍保留 10 µs 的原始参数。这证明参数和数据流程，不证明实验室物理精度。

## taiyidsl 命名与迁移验证

发行包、Python 导入、源码目录、文档和示例统一使用 `taiyidsl`；项目环境按原锁定依赖重新创建。
23 个原有 Python 文件仅替换名称后，语法树与迁移后的源码完全一致。
113 项测试通过；四组示例的 123 个扫描点驱动事件哈希、结束时间，以及 32,400 次 host 调度检查均与迁移前一致。
比较记录：`output/rename_validation/behavior_comparison.json`。
新命名的 wheel 在隔离环境安装后通过包元数据、导入、Rabi 编译和两种导出模式等价检查；记录：`output/rename_validation/wheel_smoke.json`。
新目录验证发现 pytest 的嵌套临时路径依赖已有父目录，因此 CI 和复现命令改用可直接创建的 `--basetemp=.test-tmp-pytest`。

## 默认参数化导出与调试展开导出

同一个编译产物分别导出 `mode='auto'` 和 `mode='unrolled'`，重建每个扫描点的驱动事件并与原始 compiled timeline 比对；两种代码的事件再直接比较。以下行数包含整个生成的 Python 文件，kernel 数仅计算 shot 程序，不含共同的设备初始化 kernel。

| 示例 | 扫描点数 | 默认源码行数 / shot kernel 数 | 展开源码行数 / shot kernel 数 | host 顺序检查 |
|---|---:|---:|---:|---|
| Rabi | 49 | 146 / 1 | 1221 / 49 | cyclic，14,700 shots 通过 |
| Ramsey | 41 | 143 / 1 | 1197 / 41 | sequential，12,300 shots 通过 |
| RB | 21 | 1976 / 2 | 3429 / 21 | cyclic，4,200 shots 通过 |

Rabi 的零时长点、Ramsey 的零延迟点通过可选事件槽共享同一个 kernel；RB 中无法合并的事件结构生成两个 kernel。默认优化同样适用于自定义 sweep 实验，内置模板没有专用导出分支。每个实验的 `export_comparison.json` 保存逐点检查和源码统计，`artiq/experiment.py` 为默认版本，`artiq_unrolled/experiment.py` 为调试版本。

## 明确未验证

- **真实 ARTIQ 编译：not_verified**。本机没有安装实际 ARTIQ 编译工具链。
- **实机时序、光学相位、RTIO 吞吐：not_verified**。参考设备时序预算尚未实测。
- **浏览器视觉和交互：未实测**。浏览器工具安全策略禁止打开本地 `file://` 页面，因此没有绕过限制改用其他浏览器或服务。已完成生成结构、内容与脚本语法检查；用户可直接打开 `output/tuneup_workflow/ramsey/timeline.html` 检查实际交互。

RB 的 `p_std_shot_only` 是条件于样本序列的 shot-noise 拟合不确定度，不包含随机序列间方差。原始 sequence 轴保留，后续可增加序列级 bootstrap。

## 重现命令

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
uv sync --locked
uv run --locked pytest --basetemp=.test-tmp-pytest --junitxml=output/test-results.xml
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python examples/tuneup_workflow.py
uv run --locked python examples/state_initialization.py
uv build --no-sources
```

此记录与生成的 `verification_report.json` 分开保存：后者记录每个实验的点数、参数版本、量化诊断、导出检查和拟合结果。
