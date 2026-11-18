# 从设备库到实验信号

`DeviceSetup.from_descriptor()` 只读取配置，不 import ARTIQ/spcm，也不访问仪器。
`device_db.py` 使用 ARTIQ 的标准 `device_db` 字典；`spec_db.py` 使用本项目的
`spec_db` 字典，不是 Spectrum 官方数据库格式。二者都可直接传入普通字典。
文件支持字面值、常量引用和基本数值算术，不执行 import、函数或循环；动态文件应由
用户在明确的配置准备步骤求值，然后传入得到的字典。

## 已有 ARTIQ 系统怎样接入

实验室已经在使用的 `device_db.py` 就是硬件配置来源，不需要为 DSL 重新写一份。
把它的真实路径交给 `from_descriptor()`；descriptor 仅选择这份库里的设备名。
已有 core 的 IP/主机名仍在 `device_db["core"]["arguments"]["host"]`，TTL 的
RTIO 编号仍在相应条目的 `arguments.channel`，DDS 的 SPI/CPLD 关系也继续由原库决定。
原生 GreenMOT 包和 DSL 最终使用同一份设备库快照。

例如已有并确认空闲的 `ttl6`、`ttl7` 分别接两张卡，可以直接写
`"triggers": {"A": "ttl6", "B": "ttl7"}`。若希望使用实验室接线名称，可在原有
声明式库中增加 `"awg_trigger_a": "ttl6"` 和 `"awg_trigger_b": "ttl7"` 别名；
这只给现有设备命名，不新增物理通道，也不改变原来的 RTIO 编号。是否空闲、是否与
原生准备程序共用，以及线缆实际接在哪里，需要实验室根据现有程序与接线确认。
示例中的 TTL 名称、编号与主机名不能直接当作实验室的真实配置。

当原库通过循环、函数或 import 动态生成时，静态加载会明确拒绝。由实验室在自己的
配置入口按原有方式求值得到 `device_db` 字典，再把字典传入即可；DSL 不自动执行
任意设备库源码。未用于 DSL 的设备可以继续保留，供原生程序按名称访问。

## 地址、接线与信号

地址只写在设备库里：ARTIQ 使用既有 core host；Spectrum 可以用官方驱动的本地设备
标识，或可用远程设备的标识，也可以省略 address、按真实序列号查找本地卡。具体
标识来自安装驱动后的设备枚举，并不由逻辑信号名决定。[官方 spcm 连接方式](https://github.com/SpectrumInstrumentation/spcm#connect-to-a-device)

ARTIQ 的 host 是电脑访问 core 的网络地址；RTIO channel 是 core 上的硬件通道编号，
两者不同。本地 M4i PCIe 卡的 `/dev/spcm0` 是 Spectrum 驱动标识，不是 IP，也不是
模拟输出 channel 0。官方远程标识形如 `TCPIP::192.0.2.20::inst0::INSTR`，只有驱动
实际提供相应远程设备时才使用；填写这个字符串不会让一张本地 PCIe 卡自动拥有网络接口。
TTL 触发线接卡的 EXT0，DAC 输出端口由 `channels.*.channel` 选择；外部参考时钟与
信号线、触发线也是分别接线，descriptor 本身不能证明线已经接好。

```python
spec_db = {
    "A": {
        "address": "/dev/spcm0",  # 示例标识；替换为实际卡
        "serial_number": "12345",
        "active_channels": [0, 1],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "output_range_mv": 500,
    },
    "B": {
        # 省略 address 时按 serial_number 查找，不按 A/B 名称猜测顺序
        "serial_number": "12346",
        "active_channels": [0, 1],
    },
}

descriptor = {
    "name": "laboratory",
    "backend": "artiq-spectrum",
    "instruments": {
        "artiq": {"kind": "artiq", "device": "core"},
        "A": {"kind": "spectrum", "device": "A"},
        "B": {"kind": "spectrum", "device": "B"},
    },
    "channels": {
        "nuclear_rf": {"instrument": "A", "channel": 0},
        "clock": {"instrument": "B", "channel": 0},
        "camera": {"instrument": "artiq", "device": "camera_ttl"},
        "awg_trigger_a": {"instrument": "artiq", "device": "awg_trigger_a"},
        "awg_trigger_b": {"instrument": "artiq", "device": "awg_trigger_b"},
    },
    "sync_groups": {
        "qubit_awg": {
            "cards": ["A", "B"],
            "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b"},
        },
    },
    "signals": {
        "q0": {"rf": "nuclear_rf", "clock": "clock"},
        "camera": "camera",  # 非 qubit 信号仍可单独命名
    },
}
```

`signals["q0"]["rf"]` 展开为逻辑名 `q0.rf`，与 `Qubit(signals={"rf": "q0.rf"})`
对应。旧的 `"q0.rf": "nuclear_rf"` 写法继续支持；同一逻辑名不能同时用两种写法重复
声明。分组只接受一层 `qubit → role → channel`，group 和 role 中不能再带点号。
`camera` 这样的非 qubit 信号不需要强行放进 Qubit。

`sync_groups.*.triggers` 可以引用上面声明的 TTL channel 名，旧写法直接引用
device_db 设备名也继续支持。channel 与同名设备必须解析到同一端点，否则拒绝歧义。
触发线不需要映射到 qubit signals；同步组使用期间由编译器管理，不能再手写重复触发。

若 q0、q1 真实共用公共 RF 场，可以写：

```python
descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
descriptor["signals"]["q1"] = {"rf": "nuclear_rf"}
```

这里 q0.rf 与 q1.rf 指向同一物理输出，不代表可独立寻址。实验中应使用显式公共门并
选择校准来源，例如 `ops.global_x90(q0, q1, calibration=q0)`。若两者实际独立控制，
应在 channels 中声明两个不同的物理端口，再分别映射，不能只改逻辑名称。

`instruments.*.device` 是相应数据库的条目名；一个实验室通道只声明一次物理端点。
ARTIQ 通道引用设备名，不重写 RTIO channel。别名按 device_db 解析；TTL 和 AD9910
可用于 DSL，其他设备仍可由原生包控制。AD9910 的 SPI、CPLD 和系统时钟从数据库派生。
当前按 ARTIQ 9 驱动的 `refclk/clk_div/pll_en` 规则计算 DDS 系统时钟；该驱动不接受
`pll_n` 构造参数，因此遇到这个字段会明确报错。`core` 可以是别名，但 descriptor 中
的 ARTIQ 仪器必须解析到同一 core。不同设备名不能重复声明同一个 RTIO 输出。
可选 `channels.*.timing` 保存编译所需的输出延迟等已声明时序参数，不覆盖驱动拓扑。

Spectrum 条目可指定官方 SDK 支持的 `address`（本地设备标识或远程 VISA 标识）；
打开后仍核对 `serial_number`。采样率、参考时钟、启用通道、量程等使用现有
SpectrumCard 字段的名称，当前后端仍要求 M4i.6631-x8、1.25 GS/s、125 MHz 参考。
省略值是软件默认配置，不是实测结论。同步组可选 `timing` 和 `calibration` 分别保存
编译时序约束与实测校准；一个公共触发线用 `trigger` 代替 `triggers`。

多个 Qubit 共用一个物理场时，在对应 channel 加 `sharing="global"`，并把所有覆盖
信号映射到同一个 alias。`setup.connections()` 返回信号到物理端口的行；
`setup.trigger_connections()` 返回 TTL 到卡的行。数据均是副本，修改概览不会改接线。

```python
setup = DeviceSetup.from_descriptor(
    descriptor, device_db="C:/lab/config/device_db.py", spec_db=spec_db
)
lab = LabConfig(setup, qubits=[q0], emulation=True)
compiled = lab.compile(exp)
```

Qubit 的驱动频率和门幅度进入脉冲编译，不改变输出量程、采样率或参考时钟。
显式提供给 LabConfig 的 Qubit 与实验里的同名快照必须相同。旧
`LabConfig(setup, signal_map, backend)` 和 lab JSON 仍可离线使用；旧格式导出的是
编译配置，不是可连接的数据库配置。新编译产物记录 descriptor 和数据库来源。

## 查看 DeviceSetup 与 LabConfig

```python
from pprint import pprint

pprint(setup.describe())
pprint(lab.describe())
```

`setup.describe()` 的结构如下；全部来自已读取的配置，不访问硬件：

| 字段 | 内容 |
| --- | --- |
| `name / backend` | 实验室名称与内部后端名称 |
| `databases` | 原设备库路径或字典来源，以及内容指纹 |
| `instruments` | 数据库条目、解析后的连接选择与有效配置 |
| `channels` | 实验室通道名与物理端口的声明 |
| `sync_groups` | 卡的分组、TTL 接线及显式时序／校准声明 |
| `signals` | 用户填写的按 qubit 分组或平面信号映射 |
| `connections` | 展开后的每条逻辑信号到仪器／端口的连接行 |
| `trigger_connections` | 解析别名后的 TTL 到卡连接行 |

例如 `instruments["artiq"]["connection"]` 显示 Core 驱动类和真实配置的 host；
`instruments["A"]["connection"]` 显示 `spcm.Card` 与 address；未指定 address 时，
显示实际使用的 serial_number 选择条件，不编造网络地址。Spectrum 的 `settings`
包括默认值补齐后的型号、采样率、时钟、启用通道、量程等。

`lab.describe()` 在 setup 概览外增加 `backend`、`emulation`、`artiq_python`、
`qubits` 快照以及派生的 `signal_map`。因此可以同时检查硬件接线和本次校准版本。
`describe()` 返回独立副本，修改显示结果不会改配置；`setup.to_dict()` 与
`lab.to_dict()` 仍保留原来的编译配置序列化格式，不承担完整设备库展示职责。

## spec_db 怎样到达官方驱动

1. `from_descriptor()` 读取 spec_db，把卡参数转换成内部 `SpectrumCard` 约束，
   将 address 单独保存为连接选择；此时只做校验，没有打开卡。
2. `LabConfig.compile()` 用这些参数检查采样网格、容量与时序，计算 DAC 样本。
   descriptor 的通道和信号映射决定每份样本属于哪张卡、哪个输出。
3. `connect()` 在独立 ARTIQ 环境中通过 `SpcmAdapter` 调用官方 `spcm.Card`，
   按 address 或 serial_number 打开卡，再核对身份与当前时钟。
4. `run()` 的既有 `SpectrumRunner` 做全卡预检，随后调用适配器的 `configure()`
   与 `upload()`，由官方 `Channels/Trigger/Sequence` 设置输出、触发及序列并传输样本。
   所有卡 ready 后才能执行 ARTIQ 触发 kernel。

用户编写普通 Ramsey 不需要创建这些内部对象。当前 connect/run 会检查时钟配置，
不会为了通过检查而悄悄重设时钟；实验室更改时钟配置后需重新取得同步校准。驱动负责
硬件调用，DSL 仍负责相位、分段、多卡公共时间轴和 ARTIQ 触发排程。

## 连接与执行

`LabConfig(..., emulation=False, artiq_python=...)` 仍不访问硬件。
只有 `lab.connect()` 才创建独立 ARTIQ 进程，读取已固定的 device_db 快照，通过
ARTIQ DeviceManager 连接 core，通过官方 spcm 打开卡并检查标识、时钟。
默认工具链路径是项目 `.toolchains/artiq9/clang64/bin/python.exe`；其他安装位置显式传入。
spcm 与其本机驱动需在选定的运行环境可用，不混装到离线 DSL 环境。

```python
with lab.connect(clock_session_ids=verified_clock_sessions) as session:
    receipt = session.run(compiled)
```

默认新连接产生新 clock-session ID；不能直接用校准文件中的 ID 冒充当前硬件未变。
只有实验室确实持续监控并确认时钟未重锁时，才显式提供已有 ID。校准必须绑定正确的
硬件指纹和当前时钟会话；缺少实测校准会拒绝 run，不能通过 emulation 开关绕过。
connect 不会偷偷重设时钟以使它符合配置。需要更改时钟时应先配置并重新校准。

run 导出并执行同一个编译产物；使用既有 SpectrumRunner 的全卡预检、上传、ready、
单顶层 kernel 和完成等待。退出 context 关闭设备。工作区与记录位于
`output/hardware_sessions/`，连接进程和原生代码的输出写入 worker.log。
原生包的 ARTIQ 参数管理器使用其声明的默认参数；没有默认值的必需 build 参数须在
实验室适配层明确提供。此入口不调用 ARTIQ GUI 来询问参数，也不创建 DatasetDB。

此入口当前返回**执行记录**，不接入特定相机的数据传输；GreenMOT 的相机 TTL 不是
图像数据。含 DSL acquisition 的实验会明确拒绝，不能返回伪造观测结果。实际探测器
适配器继续使用现有结果接口，须由实验室提供。发生失败的硬件 attempt 不自动重试。
