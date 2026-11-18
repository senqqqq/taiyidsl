# Qubit 参数、控制相位与标准操作

新实验统一使用 `Qubit(uid, signals=..., parameters=..., position=...)`。
`q.rf/q.clock/q.rydberg` 是逻辑信号，`q.parameters.rf` 等是该信号角色的参数。
DeviceSetup 负责将逻辑信号连接到实验室通道和物理输出；Qubit 不保存卡地址、
采样率或模拟输出量程。以下数值仅演示接口，没有物理校准含义。

新式Qubit的parameters可直接编辑，例如 `q.parameters.rf.pi2_amplitude = 0.24`。
Experiment、Section和编译产物自动保存构建时的参数副本；继续修改q不会改变旧实验。
重新调用实验函数即可采用新校准，日常赋值不需要QubitStore。

## 按信号角色定义参数

```python
from taiyidsl import Qubit, Position, QubitOperations, us, MHz, um

q = Qubit(
    uid="q0",
    signals={"rf": "q0.rf", "clock": "q0.clock", "rydberg": "q0.rydberg"},
    parameters={
        "rf": {
            "frequency": 1 * MHz,
            "pi_duration": 10 * us,
            "pi2_duration": 6 * us,
            "pi_amplitude": 0.3,
            "pi2_amplitude": 0.22,
            "waveform": "square",
            "frame": "spin",
        },
        "clock": {
            "frequency": 80 * MHz,
            "pi_duration": 20 * us,
            "pi2_duration": 12 * us,
            "pi_amplitude": 0.25,
            "pi2_amplitude": 0.18,
            "frame": "clock_control",
            "transition": {"lower": "g_up", "upper": "e_up"},
        },
        "rydberg": {},  # 先声明信号；尚未提供门所需的校准。
        "extensions": {"my_lab": {"trap_label": "A"}},
    },
    position=Position(2 * um, 3 * um, 0, "tweezer_plane"),
)

q.rf  # "q0.rf"：逻辑信号名，供 play 等 DSL 指令使用。
q.clock  # "q0.clock"
q.parameters.rf.frequency  # 数值，以 Hz 保存。
q.parameters.clock.pi2_duration
q.position  # 带坐标系的三维位置，以米保存。

ops = QubitOperations(drive="rf")
saved = ops.x90(q)  # 没有活动实验：返回独立 Section。
```

所有驱动角色使用相同的参数结构，不需要额外的 `drives` 层。
逻辑信号名必须与 DeviceSetup 的 `signals` 键一致，名称不必采用 `q0.rf` 的形式。
角色也可以由实验室命名；通过 `q.signal(name)` 和 `q.parameters[name]` 动态访问。

| 参数 | 含义 |
| --- | --- |
| `frequency` | 波形输出采用的逻辑驱动频率；经过显式配置的频率转换后作用到硬件 |
| `resonance_frequency` | 物理跃迁信息，可省略，不自动复制到 `frequency` |
| `pi_duration/pi2_duration` | 分别校准的 π、π/2 脉冲总时长，以秒保存 |
| `pi_amplitude/pi2_amplitude` | 分别校准的归一化幅度，范围为 0..1 |
| `waveform/waveform_parameters` | 波形形状及其参数；形状缺省为 `square` |
| `frame` | 与该驱动关联的控制 frame 名称，或 `None` |
| `transition` | 可选的上下能级及共振频率描述 |

新构造方式中，缺少的数值保持 `None`。例如空的 `rydberg` 参数可以保存，
但使用它构建门时，缺少必需频率、时长或幅度会报错。不会自动将 π 脉冲时长或幅度
减半作为 π/2 校准，也不会把显式零幅度当作缺省值。
若角色及其 `transition` 都填写 `resonance_frequency`，二者必须一致。

Position 是静态坐标，不自动触发寻址或搬运。实验室其它数据放在有命名空间的
`extensions` 中，例如 `q.parameters.extensions["my_lab"]`，内容需可序列化。

## 操作返回什么

`@qubit_operation` 装饰的原始函数只写 DSL，返回 `None`。装饰器执行函数体，
收集 Node 子树并返回只读 Section。有活动上下文时自动插入一次：

```python
from taiyidsl import Experiment, add

exp = Experiment("gate_example", [q])
with exp.average(10), exp.realtime():
    with exp.triggered_sequence(group="qubit_awg"):
        ops.x90(q)  # 已经插入；不要再 add(ops.x90(q))。
        ops.z90(q)  # virtual Z，后续 spin 驱动继承 frame 更新。
        add(saved)  # 将此前保存的 Section 复制插入。
        ops.clock(q, rotation="pi2", phase=0.3)
```

标准操作包括 `x90/xm90/y90/ym90`、`x180/xm180/y180/ym180`、`z90/z180`。
脉冲的 `amplitude/duration/frequency/phase`、`waveform/waveform_parameters`
可以单次覆盖，不修改 Qubit。`phase` 是该脉冲的基础相位，仍叠加所属 frame 的偏移。
Clock 必须明确选择 `rotation="pi"` 或 `"pi2"`；复杂相干转移用自定义操作组合。
编译元数据保留实际脉冲参数、Qubit fingerprint 和 revision。

需要 FlatTopGaussian 时，设置 `waveform="flat_top_gaussian"`，并在
`waveform_parameters` 中提供 `sigma`、`sample_interval` 及可选 `cutoff_sigma`。
`pi_duration/pi2_duration` 包含两侧 Gaussian 边沿，短于边沿长度时报错。
脉冲需满足硬件采样网格；segment 的 32 点长度约束由完整序列编译处理，
不用把每个门单独补齐到 32 点。

## 公共场与独立寻址

若 DeviceSetup 将一个输出声明为 `sharing="global"`，并把多个 Qubit 信号映射到它，
应显式调用公共门，并选择校准来源：

```python
ops.global_x90(q0, q1, calibration=q0)
```

这只发出一次波形，采用 q0 的 RF 参数和控制相位，记录 q0、q1 都受到作用。
它不声称 q1 也必然实现精确 X90，不合并两者的校准或 frame。公共操作也接受与
`x90` 相同的单次脉冲覆盖参数；其它驱动可通过 `QubitOperations(drive="clock")`
显式选择。

descriptor已经声明sharing="global"时，普通play或门操作无需重复targets：

```python
frequency = q0.parameters.rf.frequency * Hz + detuning
play(q0.rf, pi2_pulse, amplitude=q0.parameters.rf.pi2_amplitude, frequency=frequency)
```

编译器根据接线在排程前确定所有已配置作用对象，记录global_operation和资源占用。
校准来源由源信号q0.rf确定；只发一次波形，不合并各原子的校准或frame。
LabConfig从已登记qubit中补齐这次公共输出涉及的对象，不改写原Experiment，也不加入
无关qubit。配置引用了未登记的信号所有者时明确报错，不猜测或虚构原子。
显式targets保留为可选检查；它与接线矛盾或漏掉覆盖对象时仍报错。

当前Ramsey教程只定义并观察q0：RF仍是公共场，但未把不关心的其它原子纳入示例。
只有以后明确登记其它原子的信号映射时，后台才有依据记录它们。

## 控制 frame 的显式关联

RF/Raman 的 `frame` 缺省为 `"spin"`，关联原核自旋 `q.frame`。
其它角色缺省为 `None`。上例显式将 clock 关联到
`q.phase_frame("clock_control")`；也可用 `q.drive_frame("clock")` 取得关联。
每个 frame 内只有一个统一的偏移 F。

`rz(q, angle)` 仍是原核自旋逻辑操作，不自动获得 clock 的量子 Rz 语义。
`shift_phase` 更新所选 frame；`reset_frame` 清除它的整个偏移；`reset_phase`
还清除继承的调制偏移并重建该 frame 所关联输出的载波参考，不能打断活动脉冲。
Section 的进入、复用、嵌套或退出都不会隐式重置 frame。

独立的控制 frame 不代表物理能级之间没有相干关系。Clock 若相干转移核自旋叠加态，
应由具体操作明确 pulse phase、态映射及相位补偿；库不从驱动名称自动推导多能级
相位传播规则。

## 直接修改参数与实验保留副本

```python
q.parameters.rf.pi2_amplitude = 0.24
q.parameters.clock.frequency = 81 * MHz
q.parameters.extensions["lab"] = {"detuning_hz": 500.0}
q.parameters.extensions["lab"]["detuning_hz"] = 750.0
```

赋值发生在原来的q上，校验成功后自动更新revision和fingerprint；失败时不改变原值。
已保存的 `rf = q.parameters.rf` 也会读取当前值。标准字段保留单位、范围和拼写检查；
扩展参数使用普通JSON数据并约定单位。它们只有被实验显式读取才会影响输出。
UID、信号角色和接线身份仍保持固定；改接线需建立相应配置，避免把校准赋值误当换线。

`Experiment(..., [q])` 自动保存独立副本。若之后修改q，旧exp仍代表原来的实验；
调用实验函数建立新的exp即可。LabConfig不再用登记时的旧校准覆盖或拒绝新exp，
但继续检查信号接线身份，并从本次exp派生frame关联。已编译数据也不变。

简单保存可用 `json.dumps(q.to_dict())`，读取用 `Qubit.from_dict(...)`，新式格式的
读取结果可继续编辑。`q.snapshot()` 专门取得只读副本；普通写实验无需调用它。
旧式 `Qubit("q0", QubitParameters(...))` 继续保持原只读兼容行为，不自动迁移格式。

## 命名pulse和函数式实验

先创建命名pulse，再把它传入返回Experiment的实验函数。Ramsey的两次RF门共用
同一个pi2_pulse，在创建时保存π/2时长和幅度；play只覆盖实际RF频率和第二脉冲相位：

```python
pi2_pulse = Square(
    duration=q.parameters.rf.pi2_duration,
    amplitude=q.parameters.rf.pi2_amplitude,
)
frequency = q.parameters.rf.frequency * Hz + detuning
# 实验函数正文中的一次调用；另一次复用pi2_pulse，只改变phase。
play(q.rf, pi2_pulse, frequency=frequency, phase=phase)
```

不额外建立通用rf.amplitude，也不为前后门复制pulse。RF在实验函数开头显式计算
frequency = q.parameters.rf.frequency * Hz + detuning，两次play使用该结果。
不再同时给play传detuning，避免失谐重复相加。clock仍默认读取Qubit频率，第一pulse相位默认零。
底层play的标量detuning接口保留兼容，但教学采用上述更直观的显式频率。
修改幅度、时长或形状后，先重建命名pulse，再调用实验函数、编译并更新报告。
只改频率时无需重建pulse，但仍要重新调用函数和编译。普通函数与@experiment均如此；
pulse中的数值不会自动跟随Qubit，旧exp也不会因为重新编译而读取新的Qubit或pulse。

编译在实验函数外执行。两份 [Ramsey教程](tutorials/README.md) 完整展示
`ramsey(q0, detuning, tau, pi2_pulse, phase, ...)` 和
`clock_ramsey(q0, detuning, clock_pulse, pi2_pulse, phase, ...)`。
Ramsey只扫描tau，clock_ramsey只扫描第二脉冲phase，其余控制参数为标量；没有扫描轴自动判断。
后者间隔由clock_pulse时长决定，不再重复指定tau。

旧接口的pulse缺省amplitude/frequency解析仍兼容，但新教程不依赖它推断量子门。
标准QubitOperations也仍能按门类型读取校准；自定义操作可接受命名pulse。

## 可选的校准记录管理

需要保存历史、校准proposal和原子持久化时使用QubitStore。它保存的是只读记录，
`store.update` 返回新版本，不修改已保存记录或传入store的原始q；此进阶规则与
日常 `q.parameters... = ...` 编辑入口不同。`store.save(path)` 显式保存，配置保存
路径后也可 `update(..., persist=True)`。Proposal继续检查base revision/fingerprint。

Store路径可写成 `rf.pi2_amplitude` 或 `parameters.rf.pi2_amplitude`；extensions
支持建立新的字典命名空间，未知驱动字段仍拒绝。若要编辑从Store取出的记录，可用
`Qubit.from_dict(store["q0"].to_dict())` 建立独立工作对象，原校准记录保持不变。

新Qubit保存格式仍为schema 3，包含signals/parameters/position；“是否可编辑”
不写入文件，不改变历史fingerprint。QubitStore继续读取schema 1、2、3，读取旧文件
不自动改写，旧proposal不会因本次接口更新绕过版本检查。

## 旧接口兼容

已有 `Qubit("q0", QubitParameters(...))` 仍可使用，保留原有默认值和序列化形式。
旧式 `params.rf/params.raman`、`params.drives`、`drive_frames`、`transitions`
属于兼容数据结构；新实验使用本文开头的统一角色结构。

| 旧代码 | 新实验中的对应写法 |
| --- | --- |
| `q.params.rf` | `q.parameters.rf` |
| `q.params.drives["clock"]` | `q.parameters.clock` |
| `q.drive("clock")` | `q.parameters.clock`；旧方法仍保留 |
| `q.signal("clock")` | `q.clock`；动态访问仍可用旧方法 |
| `q.params.position` | `q.position` |
| 旧式更新路径 `drives.clock.pi_duration` | 新式对象使用 `clock.pi_duration` |

旧 Qubit 也提供只读 `q.parameters` 视图，但不会据此迁移它的保存格式。
`q.drive` 仍是读取参数的兼容方法；信号属性使用 `q.rf/q.clock/...`。
实验室术语中的 pi_length、pi_ampl 对应 `pi_duration`、`pi_amplitude`；
新接口不增加另一套缩写字段。

## 双比特操作是进阶接口

基础 Ramsey 不需要 PairCalibration 或 CZ 实现。双比特实验可由实验室提供
`QubitOperations(cz_impl=my_cz)`，其中 `my_cz` 是 `@qubit_operation` 或普通 DSL
helper。`ops.cz(q0, q1)` 返回声明两个目标的 Section；未提供实现会报错，
库不会生成具有物理 CZ 含义的默认脉冲。

PairCalibration 保存有序端点角色、实现名、参数、局部相位补偿、来源和 revision。
`calibrated` 状态必须固定两端 Qubit 的 revision 和 fingerprint；
`pair_calibrations` 中的过期快照或端点顺序不匹配会被拒绝。
局部补偿属于具体 `cz_impl` 的定义，外层只记录它，不自动叠加第二次。
`revised(expected_revision=..., parameters=...)` 返回新版本并降为未验证，
save/load 使用原子文件写入。`status="demonstration"` 不作为物理校准证据。
