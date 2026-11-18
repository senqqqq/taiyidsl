# Section、量子操作与原生实验的复用

`Experiment` 保存完整实验的 qubit、平均和扫描配置。`Section` 保存其中一段尚未
排程的 DSL 结构；`qubit_operation` 定义如何根据 qubit 和参数生成这段结构。它们
共用原有 Node 树和编译器，不构成另一套执行系统。

## 当前对象层级与 `_builder` 的含义

```text
Experiment（qubits、metadata、root）
└─ root: Node(kind="experiment")
   └─ Node(kind="average")
      └─ Node(kind="sweep") … 可嵌套 host 扫描轴
         └─ Node(kind="realtime")
            ├─ 原生 prefix / timed 调用节点（可选）
            ├─ Node(kind="triggered_sequence")（混合 AWG 所属范围）
            │  └─ Node(kind="serial", schedule=…, operation_name=…)
            │     └─ play / delay / frame / parallel / repeat / case …
            └─ 读出节点或原生 suffix
```

现有 `with exp.serial()`、`parallel()`、`repeat()` 等不是分别继承的一组 block 类：
上下文管理器在构建期维护节点栈，产物统一是 `Node`，用 `kind` 区分含义，
用 `params` 保存参数、`children` 保存内部节点。`stage()` 本来就是带名称的 serial Node。
上图只是典型布局，纯 ARTIQ 的控制节点直接放在 realtime 中。

新增 `Section` 是对一棵未排程 Node 子树的公共封装；插入时复制其根 serial Node。
它不会在 IR 外另造一套执行层级。`qubit_operation` 是产生 Section 的函数装饰器。
`@experiment` 则产生完整 Experiment，两者的返回对象和复用范围不同。

`_builder` 只是私有函数名，不是 DSL 类型。在 `dsl.py` 中，它取当前活动收集器
（Experiment 或 Section collector）；在 `staged_workflow.py` 中，同名函数校验并返回
用户传入的普通 DSL helper。都不会凭名字自动生成可复用段。以前的普通 helper
通常返回 None，作用是在当前树上追加节点；现在要显式保存和再次插入一段，用 Section。

## 直接调用操作，或先保存一段再插入

```python
from taiyidsl import (
    Qubit,
    Square,
    Experiment,
    play,
    section,
    add,
    qubit_operation,
    us,
    parallel,
    barrier,
)

q = Qubit("q")


@qubit_operation
def x90(q, pulse):
    play(q.rf, pulse)


pulse = Square(4 * us, amplitude=0.2, frequency=30_000)
saved = x90(q, pulse)  # 当前没有实验上下文：只构建独立 Section。

exp = Experiment("reuse", [q])
with exp.average(10), exp.realtime():
    with exp.triggered_sequence(group="qubit_awg"):
        x90(q, pulse)  # 活动上下文中，自动加入一次并返回 Section。
        add(saved)  # 显式加入之前保存的段；exp.add(saved) 等价。
```

这里的脉冲数值只是接口示范，`x90` 这个名字不代表脉冲已经完成实验室校准。

`with section("name") as segment` 也能保存一段：在活动实验中退出成功时自动加入
一次，在实验外则只构建独立段。段完成后只读；`to_node()`、`to_dict()` 和
`parameters` 返回独立数据。每次 add 都插入副本，不共享可变子节点。

操作的直接调用已经插入；不要为一次预期调用写 `add(x90(q, pulse))`，这样会先
自动插入、再显式插入，总共两次。需要保存时使用 `saved = x90(...)`；活动上下文
中的这次赋值也已经插入，之后 add(saved) 明确表示再插入一次。

构建函数在 host 上运行一次，只生成 DSL；不会每个物理 shot 再执行 Python。
若需要逐点变参，应传入现有 `SweepParameter` 或使用 `repeat/case`。完整
Experiment 不能嵌套；Section 不含 average、sweep、realtime、triggered_sequence
或原生调用边界。

## 操作容器与明确的 qubit 绑定

```python
class Operations:
    @qubit_operation
    def x90(self, q, pulse):
        play(q.rf, pulse)

    @qubit_operation
    def pair(self, q0, q1, pulse):
        with parallel():
            play(q0.rf, pulse)
            play(q1.rf, pulse)


ops = Operations()
```

装饰器支持普通函数和类方法，保留函数名、签名和说明。通过签名绑定，直接传入的
Qubit 形参成为目标；`pair(q0, q1, pulse)` 与关键字调用含义相同。首版不扫描
列表、字典、闭包或对象属性来猜测目标，也不支持 `*args/**kwargs` 隐藏目标。
函数应同步生成 DSL 并返回 None；由装饰器返回 Section。代码中的 parallel 需从
taiyidsl 导入。上例只是同时操作两个 qubit，不代表实现了某个双比特物理门。

Section 绑定原来的 qubit 与参数快照。插入时目标必须属于 Experiment，快照必须
相同；更换 qubit 或校准后重新调用操作工厂。不会通过替换字符串把 q0 的段改成
q1 的段。SweepParameter 沿用已有规则：名称和完整定义必须与实验扫描轴一致，
不要求是同一个 Python 对象。

## 层级和时序

新代码将串行、ASAP 和并行对齐统一写成 `section()`。例如：

```python
with section("aligned", schedule="parallel", align="end", qubits=(q0, q1)):
    with section("q0_composite"):
        ops.x90(q0, pulse)
        ops.x90(q0, pulse)
    ops.x90(q1, pulse)
```

`schedule="parallel"` 要求显式声明参与 qubit，支持 `align="start"`（缺省）或
`"end"`。每个直接子段是一个完整并行分支；相同 qubit 或实际输出不能由两个
独立分支重复占用。该 Section 是完整层边界，在 ASAP 父段中也等待此前工作，
并阻止后续工作越过它。`layer(*qubits, align=...)` 只是同一实现的兼容写法。
`segment.schedule` 返回 `"parallel"`，`segment.align` 返回所用对齐方向。

`barrier(q0, q1)` 只在 ASAP 中合流这些目标及共享资源，不包含子段，也不把
不同长度的操作向右对齐。普通串行 Ramsey 和已经用并行 Section 分层的代码
通常不需要另加 barrier。串行/ASAP Section 不接受 align，避免无效果的参数。

Section 默认串行；整个复合操作是外层 parallel 的一个分支。普通的
`exp.serial/parallel/repeat/case/stage` helper 在 Section 内仍写入当前收集器，
不会绕过它写到实验的别处。独立构建时使用不带 exp 的上下文函数。

```python
with section("gates", schedule="asap"):
    ops.x90(q0, pulse)
    ops.x90(q1, pulse)
    barrier()
    ops.x90(q0, pulse)
```

显式 ASAP 将直接子操作作为整体，在满足 qubit、输出和 frame 依赖后尽早放置；
qubit_operation 占用其全部目标 qubit 和实际输出。`barrier()` 等待本段此前全部
操作完成；`delay(t)` 在 Section 内表示整体等待；`delay(signal, t)` 只推进该
信号及关联资源的依赖链。Section 退出时与所有内部操作汇合。硬件准备时间等
约束仍由后端检查，不能实现时拒绝，不偷偷改变脉宽或显式 parallel。

Section 不建立载波参考，不重置 frame，也不保存恢复相位。内部 Rz/shift_phase 的结果
继续影响后续操作。Section 保存未排程的节点，而非已经包含绝对时刻或相位的
波形；插入后重新调度并按最终时刻解析相位。

## 公共门体

以下函数均返回 Section；在活动上下文中自动加入。它们使用普通 play，设备归属
由 signal mapping 和 triggered_sequence 决定。调用方负责准备、扫描、触发、
读出与校准。

| 接口 | 内容 |
|---|---|
| `rabi_body(q, pulse, *, drive="rf")` | 播放一次显式脉冲；其时长、幅度等可含扫描参数 |
| `ramsey_body(q, pi2_pulse, dark_time, *, drive="rf", final_phase=None, virtual_z=None)` | 两个 pi/2 脉冲及中间暗时间；可选末门绝对相位覆盖和持续生效的逻辑 Rz |
| `echo_body(q, pi2_pulse, pi_pulse, half_wait, *, drive="rf")` | pi/2、暗时间、pi、暗时间、pi/2；脉冲自带相位定义旋转轴 |
| `rb_body(q, sequence, pi2_pulse, pi_pulse, *, drive="rf", inter_gate_delay=0)` | 播放一次固定 RB gate word，保留 Clifford/recovery 描述 |

`drive` 可选 rf、raman 或该 qubit 显式声明的命名驱动；不从函数名推断物理校准。Ramsey 的 dark_time 和 echo
的 half_wait 都不包含有限脉宽。RB 使用 `taiyidsl.templates.rb_sequences` 在构建
前以明确 seed 一次生成序列，门体不重新抽取随机数；sequence 描述保存在操作
参数中。RB 按 gate word 设定 X/Y 绝对基础相位，覆盖传入 pulse.phase，其余
脉冲参数保留。实验元数据还应记录生成 seed。默认零 gap 仍须通过后端检查。

## 原生 ARTIQ 留在实验室适配层

`examples/artiq_spectrum/greenmot_native/adapter.py` 将 GreenMOT 资源列表、源码
固定和时间声明集中为 `GreenMOTAdapter`。兼容路径一次构建使用一个 NativeKernelPackage；
新增多组件接入使用 NativeModule/NativeStage/NativeProgram，见原生接入专题。
`prepare_MOT()` 是普通 helper，内联记录 prefix 与实际准备尾段 timed；
`execute_readout()` 是 suffix 代理。构建时不导入 ARTIQ 或外部 GreenMOT 源码。

这些原生代理不是可移动 Section，不能用 qubit_operation 包住。它们保持 realtime
直接子节点及 prefix → timed → 控制段 → suffix 的规则。真实 MOT、设备接线、
校准和读出仍属于实验室模块；公共库负责可复用结构、编译与执行机制。GreenMOT
示例此次仅提取适配器，保留原有门段逻辑，便于逐项比较历史行为。

说明和离线检查不能证明仪器同步、模拟稳定时间或实际门保真度。对应验证仍需
区分构建检查、编译、驱动轨迹和实测输出。

## 电路层与局部同步

默认串行的相邻 Section 按书写顺序执行。需要独立操作尽早执行时用 ASAP；需要完整
电路层时用并行 Section。它形成完整边界，不因目标不同而把相邻段重新重叠。
旧 `layer(q0, q1, align=...)` 只是同一实现的兼容包装，没有另一套容器或对齐规则。

```python
from taiyidsl import QubitOperations, barrier, section

ops = QubitOperations(drive="rf")
with section("circuit", schedule="asap"):
    ops.x90(q0)
    ops.x90(q1)
    barrier(q0)  # 仅等待 q0 及它共享的实际输出。
    ops.y90(q0)  # 可以早于 q1 的前一操作结束。
    with section("aligned", schedule="parallel", align="end", qubits=(q0, q1)):
        ops.x90(q0)
        ops.x180(q1)
    barrier()  # 当前 Section 全部汇合。
```

上例的并行 Section 先等待父段此前所有操作结束，再使其两个分支在同一时刻结束。
后续操作等待整层结束。`align="start"` 则对齐起点。复合分支内部可以有串行的多个门；
同层独立分支不能占用同一个 qubit 或同一输出，也不能操作未声明的 qubit。
没有脉冲的目标仍是这一层声明的一部分。exact_duration 仍只是断言，不自动填充时间。

`barrier(q0,q1)` 的同步包括目标关联的共享输出，因而不会漏掉由另一个逻辑名称使用
同一输出的依赖。它不输出 TTL，也不清 frame。进入或退出 Section/layer 同样不重置
相位；相位按最终调度时刻解析。

标准门库和命名驱动参数见 [Qubit 参数与操作](qubit_parameters_and_operations.md)。
