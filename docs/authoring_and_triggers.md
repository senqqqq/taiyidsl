# 实验 decorator、块级触发与独立 TTL

这些接口已经实现，并与原 `Experiment` / `exp.xxx()` 写法共用编译器。
可运行示例是 [decorated_ramsey.py](../examples/artiq_spectrum/decorated_ramsey.py)。
默认只构建、编译和导出两个扫描点，不调用仪器。

## 一整个 gate 块只有一次同步启动

```python
with exp.triggered_sequence(group="qubit_awg", trigger="qubit.start"):
    play(q.rf, p1)
    delay(tau)
    rz(q, math.pi / 2)
    play(q.rf, p2)
```

`trigger="qubit.start"` 是逻辑触发名，经 signal map 解析为同步组配置的 TTL 集合。
旧 `trigger_device="awg_trigger"` 表示公共线路；双独立线路使用
`trigger_devices={"A": "awg_trigger_a", "B": "awg_trigger_b"}`，二者互斥。
各路 TTL 在同一个 RTIO 时刻上升和结束，各卡 contract 记录自己的线路。
增加 C/D 等卡及对应 TTL 可沿用同一接口，没有两卡数量上限；实际数量受到 TTL、
时间网格和 RTIO 提交容量限制。2/3/4 卡可运行例子：
`python -m examples.artiq_spectrum.multicard_trigger --cards 4`。
逻辑触发的 fanout 必须恰好覆盖该集合，不能遗漏、重复或夹带其他输出，不能带幅频相变换。
这只保证计划时刻一致；实际两卡采样同步仍依赖参考时钟、接线和逐卡延迟校准。

当前示例的关系是：

```text
q.rf → nuclear_rf → qubit_awg / 卡 A CH0
qubit.start → awg_trigger_a → 卡 A 外触发
            → awg_trigger_b → 卡 B 外触发
```

LabConfig 中可配置 `group.trigger_signal="qubit.start"` 作为默认逻辑触发名。
省略块参数时使用该默认；旧配置没有 trigger_signal 时仍沿用 trigger_device。
真实 TTL 端口由实际 device_db 及接线确定。SIM_* 卡、脉宽与延迟示例不是实机校准。

每个有实际播放的块仍只有一次同步启动（可同时发 N 路 TTL），与 play 数量无关。纯 frame 或
没有硬件输出的块可以不发触发。编译器按块起点与既有提前量安排 TTL，而不是把
`trigger=` 理解为“第一条 gate 开始时才发 TTL”。内部 gate、delay、Rz 仍由卡上
完整静态程序自主执行，不产生逐门 TTL。

活动同步组的触发线由编译器管理，禁止同 shot 再用手动 `trig`、`play` 等控制它，
包括经另一个逻辑信号映射到同一设备的情况。独立仪器的触发必须使用独立 TTL。

## 独立数字触发 trig

```python
with exp.realtime():
    trig("scope.trigger", width=64 * ns)
```

`trig` 在当前串行时刻输出一个数字脉冲，宽度必须为正且满足所映射设备的时间网格。
它占用该输出并推进串行分支时间 **64 ns**；在 parallel 中沿用原有分支对齐和合流规则。
width 可用合法时间扫描参数。它不会隐含等待被触发设备的任务结束。

极性由 TTL 设备配置明确决定：

| `Device.ttl_idle_level` | 空闲态 | 脉冲期间 | 脉冲结束 |
| --- | --- | --- | --- |
| 0，默认 | 低 | 高 | 恢复低 |
| 1 | 高 | 低 | 恢复高 |

示例用独立 `scope_ttl`，设置 `ttl_idle_level=1`，因此输出下降沿启动的低脉冲。
ARTIQ 初始化设置声明的空闲态，每个 trig 结束恢复该电平；不能把“输出低电平”解释为不输出事件。
波形只有数字电平和持续时间，不接受模拟幅度、载频或相位。
连续两次触发之间须显式安排空闲间隔，例如 `delay("scope.trigger", 1 * us)`；
编译器不会把相邻 trig 合并成一个长脉冲，也不会自动插入间隔。
旧 `play(signal, Square(...))` 的 TTL 写法保留为高脉冲；新配置空闲高电平的输出使用 trig。

trig 可用于纯 ARTIQ 或混合 realtime，但禁止放在 Spectrum 自主执行的
triggered_sequence 内。它不会上传/arm 第三方设备、获取数据、生成 acquisition
handle、复位 qubit frame 或建立新的相位原点。需要实验采集语义时使用 acquire；
需要整段 AWG 编译和 ready 协议时使用 triggered_sequence。

## 用 decorator 省略重复的 exp.

```python
from taiyidsl import experiment, average, sweep, realtime, triggered_sequence


@experiment(qubits=[q], name="ramsey")
def ramsey(dark_time, shots=100):
    with average(shots):
        with sweep(dark_time):
            with realtime():
                with triggered_sequence(group="qubit_awg", trigger="qubit.start"):
                    trigplay(q.rf, pulse)
                    delay(dark_time)
                    rz(q, math.pi / 2)
                    trigplay(q.rf, pulse)


exp = ramsey(dark_time)
compiled = lab.compile(exp)
```

每次调用 ramsey 都创建并返回一个新的 Experiment；不在调用之间累积节点。
qubit 集合在声明 decorator 时确定，其值、parameters 和 metadata 在每次构建时
按现有 Experiment 规则快照。name 省略时取函数名；函数签名与文档保持可检查。

decorator 不自动编译、导出或执行；函数内部普通 Python 仍在 host 构建时运行，
不是 ARTIQ kernel。builder 应不返回值，返回 Experiment 由 decorator 负责。
异步、生成器及原生 kernel 函数不能充当这种 builder。

可导入的无 exp. 上下文接口包括 average、sweep、realtime、triggered_sequence、
repeat、case、stage，以及原有 serial、parallel。它们调用同一活动 Experiment
的方法，不引入新 IR 层、不改变时间单位、phase、扫描顺序或运行后端。
也可在旧 `with exp.average(...)` 内混用这些简写。

复用局部门序列可使用普通 Python helper、`section()` 或 `@qubit_operation`；
后两者提供独立捕获与快照插入，见 [Section 接口](sections_and_operations.md)。禁止在活动实验内部调用另一个已装饰的
完整实验 factory。构建异常会恢复上下文，后续构建不会接到失败实验上。
原有 with exp.xxx() 写法继续支持，stage 仍完全可选。
DSL 简写与 ARTIQ 的 delay/parallel 等名字不同义；不要在同一模块混用两者的星号导入。
原生 kernel 保持在独立模块，通过 NativeKernelPackage 声明接入。

`triggered_sequence` 内可直接使用普通 `play`；`trigplay` 保留兼容，仍只允许在块内。
它是后端归属边界，不意味着任意 DSL 指令都可由 AWG 执行：原生 ARTIQ、采集及 host sweep
仍留在块外。Spectrum-only、同一同步组的 frame 可在 realtime 外层串行位置执行
`Rz/shift_phase/reset_frame/reset_phase`。入口保留显式 frame 更新；默认从序列入口
建立零载波参考，`phase_reference="shot"` 则保留 shot T0。详见 [相位模式](spectrum_phase_modes.md)。

## 运行示例和相位参考

```powershell
uv run --locked python examples/artiq_spectrum/decorated_ramsey.py
uv run --locked python examples/artiq_spectrum/decorated_ramsey.py `
  --default-trigger --output output/artiq_spectrum/decorated_ramsey_default
```

示例复制公共 profile 到内存后添加逻辑触发映射与独立 scope_ttl，不修改原 JSON 或
原纯 ARTIQ 示例。默认与显式路径选到同一个 awg_trigger；第二条命令仅演示省略
块级 trigger 选择（传入 None 等同省略），正常使用无需重复导出两份报告。

两个点为 10 µs 和 100 ms Ramsey dark delay。每 shot 的 F 从零开始，载波默认从
AWG 序列入口推进；20 µs 的 DDS 前置脉冲不计入，序列内部 dark delay 计入。
正 Rz(π/2) 使后续驱动相位减 90°。scope 脉冲不改变这一参考，也不代表原子态复位。

报告包含完整 DSL、两卡 segment/step table、公共 AWG trigger 和独立 scope TTL
时序、pulse sheet 与最终 Spectrum DAC 样本。它不模拟 scope 模拟波形、物理 AOM
或原子态；本例没有实际采集。原生 GreenMOT 的严格准备尾段接入仍见
[native_artiq_integration.md](native_artiq_integration.md)。
