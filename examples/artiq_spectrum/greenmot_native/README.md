# GreenMOT + Ramsey 原生函数示例

`../greenmot_ramsey.py` 使用 prepare_MOT → delay → triggered_sequence → execute_readout。
prepare_MOT 是一个简短 DSL helper，记录原生准备前段和严格准备尾段两个调用；其余
名字是原生函数声明代理。构建时不会启动 ARTIQ。导出后由一个顶层 shot kernel 在
设备端串接，不在阶段之间返回 host，不需要额外 stage。

一次性声明现已集中在本目录的 `adapter.py`。`GreenMOTAdapter(source)` 复用一个
`NativeKernelPackage`，对外提供 `prepare_MOT()` 和 `execute_readout()`；实验正文
不再重复声明资源、源码校验和 prefix/timed/suffix 时长。`greenmot_stages.py`、
`greenmot_fixture.py` 中的真实 kernel 调用图保持不变。准备 helper 不加 Section，
因此原生调用仍是 realtime 的直接子节点。

实验室把此目录组织为自己的 Python 包后，可以直接导入 `GreenMOTAdapter`。
当前独立示例用显式本地路径加载这个 host 声明模块，以同时兼容直接运行和
`runpy.run_path` 离线检查；没有修改 `sys.path`，也没有因此导入或执行 ARTIQ 源码。

```python
with exp.realtime():
    prepare_MOT()
    delay(10 * us)  # 从实际准备尾段的结束边沿到第一个 RF gate
    with exp.triggered_sequence(group="qubit_awg"):
        trigplay(q.rf, pulse)
        delay(dark_time)
        rz(q, math.pi / 2)
        trigplay(q.rf, pulse)
    execute_readout()
```

已删除旧示例中独立的假想 cooling/settling 函数；严格边界来自实际 GreenMOT 最后的
关光和磁场更新，紧随其后的 delay 有明确起点。

默认 `greenmot_fixture.py` 是明确标记的原生 TTL 内核夹具，用于离线接口演示。
它不是用 play/delay 模拟的 DSL builder，也没有实现物理 MOT。默认运行示例只导出：

```powershell
uv run --locked python examples/artiq_spectrum/greenmot_ramsey.py
```

实际实验源码通过参数提供，不自动下载或导入，也不静默换成夹具：

```powershell
uv run --locked python examples/artiq_spectrum/greenmot_ramsey.py `
  --greenmot-source C:/path/to/atom-control/artiq-master/lib/_GreenMOTbase.py `
  --output output/artiq_spectrum/greenmot_ramsey_external
```

`device_db_offline.json` 仅供真实 ARTIQ 编译器的离线检查：使用正式 AD9910、CPLD、
Zotino、TTL 驱动和原程序设备名，但 RTIO 地址从 0x200 起人工分配、EEPROM 校准
替换成静态占位值，没有网络控制器或真实 core 地址。它不是实验室接线配置，不能
拿来运行硬件。离线编译会检查设备端调用链及类型，不能验证参数的物理适用性。

使用项目独立的官方 ARTIQ 工具链编译默认夹具（不运行 kernel）：

```powershell
.\scripts\artiq-python.ps1 scripts/compile_native_artiq.py `
  output/artiq_spectrum/greenmot_ramsey/export/artiq `
  --extra-device-db examples/artiq_spectrum/greenmot_native/device_db_offline.json `
  --module-root examples/artiq_spectrum/greenmot_native `
  --target rv32g --output output/artiq_spectrum/greenmot_ramsey/native_compile
```

外部源码模式将导出路径换成 `greenmot_ramsey_external/export/artiq`，并额外提供
`--module-root C:/path/to/atom-control/artiq-master`。本次验证因官方工具链没有原文件
导入的 tifffile，使用 uv 将真正的 `tifffile==2025.6.11` 独立安装到
`output/native_compile_dependencies`，再以 `--module-root` 提供该目录；没有修改
原源码、项目环境或官方 ARTIQ 环境。编译报告记录实际依赖版本和路径。
报告中的 `kernels_executed=false` 是有意的：编译通过不授权操作仪器。

本次审阅版本为 TaiyiQ/atom-control commit
`150c7fe724e8af078d97eeb8718f3e10d210d69c`，包括基础类和实际入口
`artiq-master/repository/20261109/GreenMOT.py`。本示例固定 399-horizontal 成像模式。
source hash 固定的是传入的本地文件，不代表自动证明它与某个远端 commit 字节相同。
执行导出程序的 ARTIQ Python 环境还需能导入本目录和外部 `lib` 包（将本目录与
`artiq-master` 配置到 Python 模块搜索路径），以及原文件依赖的 tifffile 等模块。
导出程序会在导入前核对路径和源码 hash；源码改变后必须重新编译导出。

原始入口的 `run()` 已经包含初始化、scan 循环、MOT、成像和收尾，因此不能整段
嵌入 Ramsey 前面。本示例保留它调用的基础函数，重新串接生命周期：

| 原程序部分 | 组合后的所在位置 |
| --- | --- |
| init_hardware，含 core.reset | initialize_native，一次性初始化 |
| scan 循环 | 由 DSL average/sweep 管理，不重复嵌套 |
| break_realtime、5 ms 等待、prepare_GreenMOT 的前段 | prepare_MOT 原生方法，控制 epoch 前的准备 |
| DRG compression/cooling | 原有 helper 原样调用；前段结束于打开 probe shutter |
| 最后的 15 ms hold → MOT 光关闭 → Zotino 磁场更新 | finish_preparation，进入严格控制窗口，保留原三个语句 |
| Zotino 最终 LDAC 上升沿 | 显式推进 16 ns 后成为“准备结束”锚点 |
| 准备结束后的 delay | 默认 10 µs，固定到第一个 gate，不能插入 break/reset |
| 新 Ramsey | 明确控制 T0 后的完整 AWG 程序，一次启动触发 |
| imaging_399_hor | execute_readout，原函数 100.48 ms，固定时序 |
| initialization | finalize_native，原有末尾清理，允许等待硬件追上 |

原 `scan_index` 只是被 print，没有赋给 f_SF 或其他频率；本示例不假称原文件已经
实现频率扫描。原生参数沿用基础类 build 的参数；Ramsey dark_time 由 DSL 扫描。
原 `print` 是可能的 RPC，留在严格 shot 内核之外；不复制原顶层 run。

原 `_GreenMOTbase.py` 不做修改。适配器只在它的最后 `delay(15 * ms)` 前切分，
没有增加或省略原有准备动作。构建时 `validate_greenmot_split()` 将适配器前段
（扣除原 run 的 break+5 ms lead）和原三个尾段语句拼回 AST，与传入的完整
`prepare_GreenMOT()` 逐语句比较；忽略注释/格式，操作、数值或顺序改变则拒绝。
这个检查证明切分结构保持，不能替代原算法或驱动的硬件验证。

官方 AD53xx/Zotino `set_dac()` 会在游标之前安排 SPI 写入，再排 LDAC 脉冲，最后
将游标恢复到调用时刻。LDAC 上升沿仍在该时刻之后两倍 SPI ref period，因此尾段
额外 `delay_mu(2 * self.zotino0.bus.ref_period_mu)`，避免把排队的最后边沿漏在范围外。
本适配器明确要求 1 ns RTIO mu、8 mu SPI ref period，声明尾段为 **15 ms + 16 ns**；
设备参数不符会报错。负向 SPI 写入位于原 15 ms hold 内。

“准备结束”指最终 LDAC 数字边沿完成；DAC 模拟建立时间、线圈电流建立时间、原子态
准备质量尚未测量。默认 10 µs 是显式实验间隔，不宣称满足这些模拟稳定指标。若还需
模拟稳定等待，应由实验指标明确给出并计入时间线，不能用未经测量的占位等待冒充。

`initialize_native` 额外明确将 ttl_Camera_Trig 置高，因为 imaging_399_hor 用下降沿
触发相机。原函数与 camera 数据获取仍不同：后者需要 host result adapter。当前
示例只保留定时触发，不编造图像结果或 qubit 测量结果。399 成像是相位不敏感读出，
末尾 virtual-Z 保存在编译产物；不把它误认为原子态已被重置。

native prefix 的墙钟时长可以随 slack 改变，图中标为原生区块。**控制相位 T0 在
严格 finish_preparation 的开始建立**，原 15 ms hold、最终边沿、准备后 delay 和
Ramsey 都计入同一控制参考。MOT 最早开始到 T0 的间隔不承诺恒定；准备结束到首门
则固定。所有 break_realtime 位于 T0 之前；严格尾段、delay、gate、readout 中禁止
break/reset/wait_until_mu。原 cleanup 的等待留在读出之后。

当前示例触发提前量为 512 ns。若准备后 delay 短到需要把启动触发倒排进原生尾段，
导出会明确拒绝，不偷偷移动 gate、增加 delay 或事后补发触发。

在默认示例中，第一个 30 kHz / 200 µs 方波从 T0 + 15.010016 ms 开始，首样本
默认 sequence 载波参考相位为 0°；显式 phase_reference="shot" 才保留108.1728°。
`rz(q, pi/2)` 使后续驱动相位减去 90°；第二个门仍包含前面的真实等待时间：

| dark_time | 第二个门开始（相对 T0） | 第二个门首相位（mod 360°） |
| --- | --- | --- |
| 10 µs | 15.220016 ms | 18° |
| 1 ms | 16.210016 ms | 270° |
| 100 ms | 115.210016 ms | 270° |

这是输出样本的编译参考，不表示每个门都重新清零载波相位。每 shot 从同一控制
参考重放，前级准备长度不进入默认 sequence 参考；序列内部等待仍累积相位。
两次 200 µs 脉冲只是结构示范，旋转角仍需实验标定。每个点只有一个公共启动触发，
最终 F=-π/2，Rz 来源诊断为 +π/2。

运行真实仪器还需要实际 device_db、SDK/参考时钟与校准、符合接线的独占 AWG
触发端口及确认过的 GreenMOT 参数。默认 lab_simulated.json 的 SIM_* 卡和延迟只
用于离线导出。这个集成示例不额外实现光镊装载、态制备或成像分析；相关物理过程
应来自实验室自己的原生函数。
# 多组件与导入

`adapter.py` 是普通可导入的 host 声明模块；从项目根可使用
`from examples.artiq_spectrum.greenmot_native.adapter import GreenMOTAdapter`。
它不导入 ARTIQ 或原始 GreenMOT 类。示例运行建议使用
`uv run python -m examples.artiq_spectrum.greenmot_ramsey`。

本适配器继续保留真实 GreenMOT 的单包调用链、初始化 reset 和逐 shot 收尾。
需要多个独立原生类时参见相邻 `native_stages/` 与
`docs/native_components.md`，无需把新模块继续塞进 GreenMOTStages。
