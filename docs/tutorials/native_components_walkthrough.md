# 原生 ARTIQ：先写源码，再声明时间边界

这一节是主 notebook 生成器的正式输入，包含两个可提取的 Python cell。
前置条件：主教程已经创建 `SESSION`（本次输出目录的 `Path`）、
`lab`、`q0`、`q1` 和 `ops`；本节复用它们，不重建项目根目录或实验室配置。

`native.GM_cooling(...)` 调用的是 DSL 代理，只记录一次将来的原生调用。
它不会在 notebook 中调用 `@kernel`、连接 core 或操作 TTL。下面的 TTL
仅演示组件边界，不代表真实 MOT、冷却、态制备或图像采集。

## 现有 GreenMOT 与四组件入口

| 入口 | 当前实现 | 生命周期与用途 |
|---|---|---|
| `examples/artiq_spectrum/greenmot_ramsey.py` | `GreenMOTAdapter` 内部使用 `NativeKernelPackage`，一个原生类接入生成程序 | 保留原实验初始化和原生方法；`finalize_method` 是**每 shot** 收尾 |
| `examples/artiq_spectrum/native_components.py` | `NativeProgram` 组合 `mot`、`cooling`、`state`、`readout` 四个 `NativeModule` | 四个独立 `HasEnvironment` 实例，共用 managers/core；`initialize` 会话一次，`after_shot` 每 shot，`finalize` 会话结束一次 |

现有 `native_stages/` 是独立的 TTL 教学适配例，没有将真实 GreenMOT 自动
拆成四个物理过程。GreenMOT 默认也是 fixture；只有显式传入外部
`_GreenMOTbase.py` 才审计真实源码。两种接入都通过一个顶层 point kernel
在 core 端依次调用原生方法和生成的控制段，并非 PC 逐段启动 kernel。

真实 GreenMOT 的准备已经包含冷却：适配器将原 `prepare_GreenMOT()` 的
最后三句拆成严格尾段，再通过 AST 检查“prefix + 原始尾段”仍与上游函数
逐语句一致。尾段最后是 Zotino 更新；显式补到 LDAC 最后上升沿后，声明
时长为 `15 * ms + 16 * ns`。该边界是数字更新完成，模拟稳定时间另行声明；
真实尾段还要求 1 ns mu 和 8 mu SPI ref period。无需再加一段虚构 cooling。

## Cell 1：保存完整的原生源码

本例将四个类放在同一个 `stages.py`，便于完整阅读；以后可移至单独模块，
同步修改 `sources` 映射即可。包中不添加自动执行实验的顶层代码。

```python
import textwrap

source_root = SESSION / "native_sources"
package_dir = source_root / "tutorial_native"
package_dir.mkdir(parents=True, exist_ok=True)
(package_dir / "__init__.py").write_text(
    '"""Native tutorial package; imported only in the ARTIQ environment."""\n',
    encoding="utf-8",
)
native_source = textwrap.dedent("""\
from artiq.experiment import HasEnvironment, delay, kernel


class MOT(HasEnvironment):
    def build(self, prefix_duration=40e-6):
        self.setattr_device("core")
        self.setattr_device("tutorial_prepare")
        self.prefix_duration = prefix_duration

    @kernel
    def initialize(self):
        self.tutorial_prepare.off()

    @kernel
    def prepare(self):
        self.core.break_realtime()
        self.tutorial_prepare.on()
        delay(self.prefix_duration)

    @kernel
    def after_shot(self):
        self.tutorial_prepare.off()

    @kernel
    def finalize(self):
        self.tutorial_prepare.off()


class Cooling(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("tutorial_prepare")

    @kernel
    def GM_cooling(self, duration):
        self.tutorial_prepare.on()
        delay(duration)
        self.tutorial_prepare.off()


class StatePreparation(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("tutorial_prepare")

    @kernel
    def state_prep(self, duration):
        delay(duration)
        self.tutorial_prepare.off()


class Readout(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("tutorial_camera")
        self.last_frame = 0.0

    @kernel
    def initialize(self):
        self.tutorial_camera.off()

    @kernel
    def readout(self, duration):
        self.last_frame = self.native_terminal_f_q0
        self.tutorial_camera.on()
        delay(duration)
        self.tutorial_camera.off()
""")
(package_dir / "stages.py").write_text(native_source, encoding="utf-8")
```

`build` 是主机构造阶段，只绑定设备和静态参数，不操作硬件。
`initialize` 等带 `@kernel` 的方法在此仍然只是源码文本。设备名采用
字面量，如 `setattr_device("tutorial_prepare")`，使审计器能够静态识别设备。
不要改成在循环中调用 `setattr_device(device_name)`，也不要在 kernel 内
动态 `getattr` 查找设备或方法。新组件继承 `HasEnvironment` 或
`EnvExperiment`，沿用标准构造器，不自定义 `__init__`。

`StatePreparation` 只是保持 TTL 低电平并等待的教学方法，不声称制备了
原子态。`Readout.last_frame` 演示取得 `q0` 控制结束时的驱动 frame；记录
该数值不等于实现了相位敏感的物理探测。主教程使用 `q0`/`q1`，因此这里
使用 `native_terminal_f_q0`；若实际 frame 名不同，须按导出报告
`control_attributes.terminal_frames` 提供的准确属性名修改源码。

## Cell 2：声明源码、资源、方法与生命周期

`source_root` 是目录变量，**不是** `NativeModule` 的参数。`sources` 的键
是未来可导入的模块名，值是刚保存的文件；构造声明时固定绝对路径和
SHA-256。包含调用链依赖的所有用户源码及包 `__init__.py`。此处不需要
`import tutorial_native.stages`，也不需要将 `source_root` 加入 `sys.path`。

```python
import sys
from taiyidsl import NativeModule, NativeProgram, NativeStage, us

pinned_sources = {
    "tutorial_native": package_dir / "__init__.py",
    "tutorial_native.stages": package_dir / "stages.py",
}
modules = {
    "mot": NativeModule(
        module="tutorial_native.stages",
        class_name="MOT",
        sources=pinned_sources,
        resources=("tutorial_prepare",),
        build_kwargs={"prefix_duration": 40e-6},
        initialize="initialize",
        after_shot="after_shot",
        finalize="finalize",
    ),
    "cooling": NativeModule(
        module="tutorial_native.stages",
        class_name="Cooling",
        sources=pinned_sources,
        resources=("tutorial_prepare",),
    ),
    "state": NativeModule(
        module="tutorial_native.stages",
        class_name="StatePreparation",
        sources=pinned_sources,
        resources=("tutorial_prepare",),
    ),
    "readout": NativeModule(
        module="tutorial_native.stages",
        class_name="Readout",
        sources=pinned_sources,
        resources=("tutorial_camera",),
        initialize="initialize",
    ),
}
native = NativeProgram(
    modules=modules,
    stages={
        "prepare_MOT": NativeStage("mot", "prepare", timing="prefix"),
        "GM_cooling": NativeStage(
            "cooling",
            "GM_cooling",
            timing="timed",
            duration=20 * us,
            end_boundary="tutorial_prepare falling edge",
        ),
        "state_prep": NativeStage(
            "state",
            "state_prep",
            timing="timed",
            duration=8 * us,
            end_boundary="end of the declared low-level hold",
        ),
        "readout": NativeStage(
            "readout",
            "readout",
            timing="suffix",
            duration=10 * us,
            phase_sensitive=True,
            end_boundary="tutorial_camera falling edge",
        ),
    },
    shared_resources={"tutorial_prepare": ("mot", "cooling", "state")},
)
assert "tutorial_native.stages" not in sys.modules
```

- `resources` 是静态设备/共享资源声明，须与真实使用对应。core 自动共享，
  不用列入。原生资源与生成控制的输出、共享总线分离，尤其不能复用 AWG
  trigger TTL；更换 device_db 别名也不能绕过运行前身份检查。
- 三个准备组件共用一个 TTL，显式列出全部参与者。只有 `mot` 是该资源的
  初始化 owner；另外两类不声明 initialize。若两个共享者都声明并选中
  initialize，Program 会拒绝。独立相机 TTL 则由 readout 初始化。软件
  缓存属于各自对象，不会随共享设备自动同步。
- `build_kwargs` 只能放静态可序列化配置，不能放 sweep。stage 实参可以是
  标量、列表、单位量或 sweep；声明的精确 `duration` 不会从实参自动推导。
  这里调用 `native.GM_cooling(20 * us)` 才与 `duration=20 * us` 一致。
- stage 需要固定位置签名；也可写 `native.GM_cooling(duration=20 * us)`，
  代理绑定后生成静态位置调用。不支持 `*args`、`**kwargs` 和 keyword-only
  参数；缺省值必须可由 AST 读取为字面值。
- `host_prepare="方法名"` 可显式选择一次主机准备 hook；未声明的
  `prepare/run/analyze` 不会自动调用。它与 build 都是受信主机代码。
  Program 的 `initialize/after_shot/finalize/host_prepare` 可用组件名 tuple
  指定顺序或子集，默认按 modules 声明顺序选择已声明 hook。

## 在主教程的实验中调用

后续 cell 复用主教程的 `lab`、`q0`、`q1` 和 `ops`。不要再次创建旧的
`Qubit("q")` 或载入只映射旧信号名的配置，否则 `native_terminal_f_q0` 与
控制段的 frame 不对应。这份输入不额外提供独立实验 cell；主 notebook
负责完整的 Experiment 构建、compile 和 export。

在 realtime 的直接子层依次调用 `native.prepare_MOT()`、
`native.GM_cooling(20 * us)`、`native.state_prep(8 * us)`，再安排显式 gap、
以 `q0` 为对象的控制段，最后调用 `native.readout(10 * us)`。所有原生
代理必须是 realtime 的直接子节点，不能藏在 Section、parallel、repeat、
case 或 triggered_sequence 内，也不能在两个 DSL gate 之间插 native stage。

时间顺序是 `prefix* → 组合器恢复 slack/建立 T0 → timed* → DSL 控制 → suffix*`。
prefix 耗时未知：即使包含 `40e-6` 延时，也不能把含 break_realtime 的整体
当成固定时长。每个 timed/suffix 返回时，生成 kernel 用实际游标差检查声明
时长是否精确相等；过长和过短都报错，**不会补等待**。静态审计不证明实测时长。

timed/suffix 的用户调用图禁止 reset、break_realtime、阻塞等待、RPC 和
动态调用。新组件的 initialize/after_shot/finalize 也不能自行 reset；reset
由根程序负责。旧 GreenMOT package 的初始化约定仍保留，不应机械套用新
组件 lifecycle。显式 gap 还要容纳 AWG 提前触发：若 trigger/preload 必须
侵入不透明 native timed 区间，编译器拒绝，不移动门或插入 gap。

默认 sequence 相位参考从 triggered_sequence 入口开始，前面的准备和 gap
不计入该载波原点；它不是量子态初始化。读出若需要控制终态相位，使用
`native_terminal_f_q0`（完整持久驱动 frame F），不要再减一次 Rz 贡献。

## 导入路径与离线编译的交接

构建与导出读取 AST/hash，不导入真实 ARTIQ 或原生模块，不调用原生 build、
initialize 或 kernel。导出文件不复制用户原生包，也不自动将 source_root
加入 sys.path。真正加载导出程序时，必须让 **tutorial_native 的父目录**
可被 Python 导入，即 `SESSION / "native_sources"`，而不是 package_dir。

以主 notebook 实际选用的 export 目录名为准，路径关系如下：

| 用途 | 本次 SESSION 下的路径模板 |
|---|---|
| 原生源码 import root | `SESSION / "native_sources"` |
| 原生源码包 | `SESSION / "native_sources/tutorial_native"` |
| Ramsey 混合导出目录 | `SESSION / "ramsey_export"` |
| `compile_native_artiq.py` 的 export 实参 | `SESSION / "ramsey_export/artiq"` |
| `--module-root` | `SESSION / "native_sources"` |
| `--extra-device-db` | `SESSION / "extra_device_db.json"` |
| `--output` | `SESSION / "artiq_compile_ramsey"` |

额外 device_db 显式声明 `tutorial_prepare` 和 `tutorial_camera` 的断开编译设备；
不能使用仪器连接信息。在本次 SESSION 目录下将以下内容保存为
`extra_device_db.json`（UTF-8，无 BOM）：

```json
{
  "tutorial_prepare": {
    "type": "local", "module": "artiq.coredevice.ttl", "class": "TTLOut",
    "arguments": {"channel": 20}
  },
  "tutorial_camera": {
    "type": "local", "module": "artiq.coredevice.ttl", "class": "TTLOut",
    "arguments": {"channel": 21}
  }
}
```

20/21 是断开编译占位通道，不能当作实验室接线。按项目
[ARTIQ 工具链说明](../artiq_toolchain.md)准备独立编译环境后，从项目根运行：

```powershell
# 从 notebook 首个 cell 复制本次输出目录，替换下方占位路径。
$lesson = 'C:/AIproject/taiyidsl/output/tutorial_from_scratch/runs/<本次目录>'
.\scripts\artiq-python.ps1 scripts/compile_native_artiq.py `
  "$lesson/ramsey_export/artiq" `
  --output "$lesson/artiq_compile_ramsey" `
  --target rv32g --point 0 `
  --extra-device-db "$lesson/extra_device_db.json" `
  --module-root "$lesson/native_sources"
```

RB 的各块目录与点号从 `rb_artifact/store.json` 查找，例如第二块的
`chunks/1/artiq`、局部 point 5 可能对应全局 point 23，不能混用编号。
编译报告中的 source/ELF hash 只证明被编译的那个确切产物，重新生成后要重新核对。

项目已有
`scripts/artiq-python.ps1` 与 `scripts/compile_native_artiq.py`，可在独立
ARTIQ 进程中编译。脚本局部加入 module root，构造 `host=None` 的设备库，
调用真实 `Core.compile` 保存 ELF，不执行 run 或 kernel。编译对象构造仍
会导入用户模块并执行受信 host build，因此 build 必须保持无仪器动作。

导出 loader 在导入类之前检查 pins 的 hash、模块解析路径和缓存。修改源码
或移动路径后，重新构造声明并导出；已有未验证或过期缓存时，使用新的 ARTIQ
worker/进程。将来由实验室启动器执行时，同样须为 worker 提供 import root，
例如正规安装实验室包或由启动器局部设置路径，且解析路径须匹配导出 pins。

分别记录 AST/hash 审计与导出、mock 轨迹、真实 ARTIQ 编译、仪器时序及
物理输出验收。软件层验证不能替代仪器验收，本节不执行任何仪器操作。
