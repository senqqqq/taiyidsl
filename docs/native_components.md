# 多个原生 ARTIQ 组件

`NativeModule` 声明一个已有 ARTIQ 类，`NativeStage` 声明一个入口方法及其时间边界，
`NativeProgram` 将多个模块组成一次实验。MOT、冷却、态制备和读出可以分别属于不同
module/class；生成程序为每个组件建立独立 HasEnvironment 子实例，组件不共享同一个
`self`，也不通过多继承合并用户类。

完整离线示例是 `examples/artiq_spectrum/native_components.py`。在项目根运行：

```powershell
uv run --locked python -m examples.artiq_spectrum.native_components
```

`native_stages/` 中的四个类只是 TTL 时序演示，不代表真实 MOT、冷却、态制备或相机
数据传输。主库不保存这些实验室具体动作。自己的实验可正常导入自己的声明包，例如
`from my_lab.declarations import native`，而不是复制导出器或使用 runpy 构建接口。

## 声明一次，直接调用

```python
from taiyidsl import NativeModule, NativeStage, NativeProgram, us

cooling = NativeModule.from_package(
    root=lab_source_root,
    entry="my_lab.cooling:Cooling",
    resources=("cooling_ttl",),
    build_kwargs={"mode": "green"},
    initialize="initialize_devices",
    after_shot="restore_idle",
    finalize="finish_session",
)
native = NativeProgram(
    modules={"cooling": cooling},
    stages={
        "GM_cooling": NativeStage(
            "cooling",
            "cool",
            timing="timed",
            duration=20 * us,
            end_boundary="最后一个准备 TTL 下降沿",
        ),
    },
)

with exp.realtime():
    native.GM_cooling(20 * us)
    delay(10 * us)
    with exp.triggered_sequence(group="qubit_awg"):
        gate_body()
```

方法调用的关键字按固定原生签名绑定，导出为静态位置调用。原生入口不能使用
`*args/**kwargs` 或 keyword-only 参数；默认值须为可静态读取的字面值，复杂缺省值
可放在 build 中，或调用时明确传入。stage 参数允许现有标量、列表、单位量和
SweepParameter；`build_kwargs` 只接受静态、有限、可序列化的配置，不接受 sweep。

`root` 是包含顶层 `my_lab/` 包的目录，例如独立实验室包的 `src/`，不是
`my_lab/` 本身。工厂会沿本地静态 import 收集源码和包初始化文件，不复制环境中的
ARTIQ 或第三方库。根目录之外的用户代码通过
`source_overrides={"existing_lab.base": existing_base_path}` 显式提供。
未找到入口、非法 `module:Class`、缺失的相对导入和动态加载器会明确报错。
特殊加载方式仍可用 `NativeModule(module=..., class_name=..., sources=..., ...)`
提供审核过的完整源码映射；其余参数和生命周期完全相同。

声明和编译阶段读取源码及 AST，不导入 ARTIQ 或用户原生模块。导出程序在受控 ARTIQ
环境中验证源码 hash 和模块来源后才导入这些类。组件应继承 HasEnvironment 或
EnvExperiment，沿用标准构造器，build 只绑定设备和主机参数，不操作硬件。已有类有
不兼容入口时，使用小型实验室适配类暴露合适的方法；无需把所有模块合并成一个类。

## 顺序和严格时序

顺序固定为 `prefix* → timed* → DSL 控制 → suffix*`，原生调用都是 realtime 直接
子节点，不放进 Section、parallel 或 triggered_sequence，也不支持门间 native 交错。

- prefix 可恢复 slack，时长不假装已知；准备结束到首门的精确间隔应由实际 fixed-duration
  尾段界定，而不是由 prefix 返回时刻推断。
- 组合器在全部 prefix 之后恢复 slack 并建立 T0，随后串行执行所有 timed。
- 每个 timed/suffix 返回时检查精确游标时长，缺时和超时均拒绝，不补 gap。
- timed 及 suffix 的调用图禁止 reset、break_realtime、blocking wait、RPC 和动态调用。
- 最后 timed 后的显式 delay 定义准备结束到控制段的间隔。提前 trigger/preload 若侵入
  不透明 timed 区间则拒绝；不会把触发向后挪动掩盖该问题。

结束游标或数字边沿不等于模拟稳定或量子态制备完成；实验室声明须明确包含哪些等待。

## 生命周期和换块

| 阶段 | 执行次数 | 声明 |
|---|---|---|
| build | 每个 native session 一次 | 标准 HasEnvironment.build 与静态 build_kwargs |
| host_prepare | 一次，明确选择 | NativeModule(host_prepare="...") |
| initialize | shots 前一次 | NativeModule(initialize="...") |
| after_shot | 每个完成的 shot 后 | NativeModule(after_shot="...") |
| finalize | session 结束一次 | NativeModule(finalize="...") |

Program 默认按 modules 的声明顺序调用已指定 hook，也可用同名 tuple 参数选择顺序和
子集。没有声明的 prepare/run/analyze 不会自动调用。初始化由根程序负责 reset；新组件
初始化、逐 shot 方法和收尾不得自行 reset。initialize/finalize 失败的 context 不可重试
或绑定到下一块，必须结束该 session 并显式处理恢复。

生成类支持 `GeneratedExperiment(managers, native_context=context)`。首块创建
`instance.native_context`；以后只绑定同一个 context，保留组件状态及 once 标记。
初始化调用 `initialize_native_once()`，会话结束调用 `finalize_native_session()`。
生成的普通 run 包含 finally 收尾；流式执行器管理全部 chunk 时由执行器结束 session。
context 会校验 program 指纹、core 身份和已结束状态，不能用于不同配置的实验。

组件参数名自动加上 namespace，例如两个组件内部都使用 `duration`，对 ARTIQ 参数
管理器分别呈现 `mot.duration`、`cooling.duration`。组件的属性名仍为 self.duration。
固定 host 配置优先用 build_kwargs；共享 dataset 名称仍由实验室代码明确管理。

## 共享设备和相位上下文

同一 core 自动共享。不同 native 组件可按顺序使用同一设备，必须在 Program 显式写：

```python
shared_resources = {"preparation_ttl": ("mot", "cooling", "state")}
```

列表须覆盖声明该资源的全部组件；共享资源只能有一个被选择的 initialize owner。
实际设备、别名、共享总线的身份还会在 host 检查，不能用不同 device_db 名称绕过。
不同组件的私有软件缓存不会自动同步，每个 stage 必须设置它所依赖的实际状态。
native 与编译控制的输出/总线继续要求分离，尤其是 AWG trigger。

每次 stage 调用前，组合器将 T0、物理基准和当时有效的终态 frame 属性写入该组件。
相位敏感读出显式声明 `phase_sensitive=True`，读取 `self.native_terminal_f_<frame>`。
F 是完整驱动 frame，不再次扣除 Rz 诊断项。非 Python identifier 的 frame 使用稳定
hex 编码属性名；导出报告的 control_attributes.terminal_frames 给出准确映射。
组件不得在 build 中定义这些保留属性。

## 兼容性与离线验证

旧 NativeKernelPackage 与 package.kernel() 继续可用；旧 finalize_method 保持原来的
每 shot 收尾含义。GreenMOT 原始源码路径保留这种接入，不改写其初始化 reset 或真实
准备/读出方法。GreenMOT 声明已能通过 package import 使用，脚本仍保留对历史 file/runpy
启动器的局部 host 声明加载兼容，不修改全局 sys.path。

新的多组件示例在独立 ARTIQ9 环境中离线编译：

```powershell
.\scripts\artiq-python.ps1 scripts/compile_native_artiq.py `
  output/artiq_spectrum/native_components/artiq `
  --output output/native_components/artiq_compile --target rv32g `
  --extra-device-db examples/artiq_spectrum/native_stages/device_db_offline.json `
  --module-root examples/artiq_spectrum
```

脚本只采用断开连接的合成 device_db，编译初始化、点 kernel 和 session finalizer，
不执行 kernel。AST 审计、mock 调用轨迹、真实 ARTIQ 编译和仪器验收是不同层次。
