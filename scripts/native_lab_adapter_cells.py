"""Notebook cells for adapting the reviewed atom-control source tree."""

# Chinese notebook paragraphs and source templates retain their visible line breaks.
# ruff: noqa: E501
from textwrap import dedent


def adapter_cells():
    cells = []

    def add(kind, source):
        cells.append((kind, dedent(source).strip()))

    add(
        "markdown",
        """
        ## 用实验室原代码组成同一个 shot

        这一节是可选的真实源码练习。在开头将 `ATOM_MASTER` 指向自己的
        `atom-control/artiq-master` 后再运行。本节只读源文件，把适配层写入本教程的
        `output/`，不会导入 ARTIQ、执行原实验的 `run()` 或连接仪器。
        这些代码可以在自己的实验室包中维护。复制前先打开下表的源码段，核对边界。

        | 本教程阶段 | 这份固定版本中实际执行的代码 | 需要从原实验入口移出的部分 |
        |---|---|---|
        | MOT | `GreenMOT.run` 的一次装载调用基类 `prepare_GreenMOT()` | 扫描、成像和循环后的复位由新的 shot 统一组织 |
        | GM | `GrayMolasses.py` 的类名是 `StatePrep`，`run()` 第 60–68 行才是 GM 脉段：等偏置场、556 光照 4 ms、关光、恢复 EW 场后再等待 | 外围的第二次装载、TOF 和成像 |
        | 556 态制备 | 同目录 `Stateprepartion_Spectrum.py` 第 209–217 行包含实际运行的光抽运：施加偏置、等待、设置探测 AOM、开光、关光、等待消光 | 后面的 RF 门与 blow-away 有独立物理作用；本例只取光抽运，须单独评估目标态布居 |
        | 578 nm 探测 | 在混合后端用 DSL 产生探测波形 | `Stateprepartion.py` 本身是原生 578 DDS 钟谱；其 `_shot` 明确跳过光抽运、RF 门和 blow-away |
        | 399 nm 读出 | 取原钟谱 `_shot` 第 187–193 行：TOF、恢复偏置、等待、设置 399 探测 DDS、水平三帧和收尾 | 一次一组，实际图像、帧 ID 与数据提交仍由采集端负责 |

        下列代码从经核对的文件提取原句，以便保留 DDS、Zotino 与快门的指令顺序。指令起止行严格对应提交
        `e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e`。
        对于新的生产版本，请重新逐段比对，确认物理顺序后再更新源指纹、起止行及验证记录。
        [MOT](https://github.com/TaiyiQ/atom-control/blob/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/repository/20261120/GreenMOT.py)
        · [GM](https://github.com/TaiyiQ/atom-control/blob/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/repository/20261120/GrayMolasses.py#L60-L68)
        · [光抽运](https://github.com/TaiyiQ/atom-control/blob/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/repository/20261120/Stateprepartion_Spectrum.py#L209-L217)
        · [原生钟谱与读出](https://github.com/TaiyiQ/atom-control/blob/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/repository/20261120/Stateprepartion.py#L170-L193)
    """,
    )
    add(
        "code",
        r"""
        import ast
        import hashlib
        from pathlib import Path
        from textwrap import dedent, indent

        date_folder = Path("repository/20261120")
        base_file = Path("lib/_GreenMOTbase.py")
        gm_file = date_folder / "GrayMolasses.py"
        pump_file = date_folder / "Stateprepartion_Spectrum.py"
        clock_file = date_folder / "Stateprepartion.py"
        reviewed_blobs = {
            base_file: "91f2d9e606b8818c43aca8e4b219abf1c2bf6c68",
            Path("lib/__init__.py"): "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
            date_folder / "GreenMOT.py": "74059af6ba809801945165ec43fa265a40302ab3",
            gm_file: "1638d23b02eb5362dc43c11b8b3667a7a3579365",
            pump_file: "4bc3b56983ef084a83e7c07932e993501218b5d3",
            clock_file: "eb5702e62d4a5ffd7f782a651ec92eba8a322cc3",
        }


        def reviewed_sources(master):
            sources = {}
            for path, expected in reviewed_blobs.items():
                source = (master / path).read_text(encoding="utf-8")
                data = source.encode("utf-8")  # Git blob 采用 LF；支持 Windows checkout
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data)
                if blob.hexdigest() != expected:
                    raise ValueError(f"{path} 已变化，请重新审核其调用和对应行。")
                sources[path] = source
            return sources


        def region(source, first, last):
            return dedent("\n".join(source.splitlines()[first - 1:last])).strip()


        if ATOM_MASTER is not None:
            atom_sources = reviewed_sources(Path(ATOM_MASTER))
            print(region(atom_sources[pump_file], 209, 217))
        else:
            print("如要生成真实源码适配层，请先设置 ATOM_MASTER。")
    """,
    )
    add(
        "markdown",
        """
        **一处初始化，共享偏置场。** MOT 继承原基类；只将 `init_hardware()` 开头的
        `core.reset()` 移交给生成实验的根初始化，组件用 `break_realtime()` 为随后的 CPLD 首次写入恢复余量，保留其余设备初始化及等待。
        水平相机以下降沿触发，初始化时将 TTL 置高；556 探测开关在首次抽运前要能消光。
        其他两个组件只绑定自己实际驱动的器件。原基类的 EW 基准在三者中使用同一个值。
        示例从源码的参数声明读取默认值，再通过 `build_kwargs` 固定。迁移实际实验时，应使用该次运行的参数记录替换这些起点。
        例如某次 Dashboard 改过的 GM 频率不会出现在源码默认值里。

        三个准备段均放在 `prefix`。MOT 内部有 `break_realtime()`；GM 和光抽运除显式等待外还有驱动开销。
        最后一个 prefix 后，编排器重新获得调度余量并建立控制时间原点。若要规定“实际抽运关光到 578 光到原子”的间隔，
        需要拆出末段为有界的 `timed` stage，并实测设备、光路和偏置场的响应。
        这里的 `guard` 在触发启动的 AWG 波形内部产生静默前导。

        读出需要在 578 脉冲后执行 TOF、Zotino 和 DDS 配置。这些操作的总耗时还没有经验证的合约，
        因而本节把整段安排在每个完成 shot 的 `after_shot` 中，沿用上游次序。
        它在每点的顶层 kernel 内执行一次。不要再叠加教学 TTL 的 `native.readout(...)`。
        若要把第一次成像沿严格锁定在某个时间，应另外设计和验证 `suffix` 的固定时长及其起始状态。
    """,
    )
    add(
        "code",
        r'''
        def write_stage_adapter(sources, target):
            # 保留原文件，导入基类；将两个内联阶段和一个收尾段写到自己的模块中。
            # MOT.build 沿用原设备绑定，统一 EW 偏置并保存每次读出前使用的 TOF 秒数。
            # MOT.load 负责一次装载，保留原入口的调度余量和 5 ms 前导。
            header = """\
        from artiq.experiment import HasEnvironment, kernel, delay, MHz, dB, ms, us
        from lib._GreenMOTbase import _GreenMOTbase

        class MOT(_GreenMOTbase):
            def build(self, ew_v, tof_s):
                super().build()
                self.ComField_EW_for_556MOT = ew_v
                self.tof_s = tof_s

            @kernel
            def load(self):
                self.core.break_realtime()
                delay(5 * ms)
                self.prepare_GreenMOT()

            @kernel
            def initialize_devices(self):
        """
            # 确认被替换的仅为原首句 reset，后面的板卡与 DAC 初始化逐句沿用。
            assert region(sources[base_file], 62, 62) == "self.core.reset()"
            initialize = "self.core.break_realtime()\n" + region(sources[base_file], 63, 91)
            initialize += "\nself.AOM_556_Probe.cfg_sw(False)\nself.AOM_556_Probe.sw.off()"
            initialize += "\nself.ttl_Camera_Trig.on()\ndelay(10 * us)"
            # 旧 _shot 把 tof_s 当实参；新 after_shot 没有实参，改从实例取同一个值。
            after = "tof_s = self.tof_s\n" + region(sources[clock_file], 187, 193)
            # 两个切片均只取本阶段操作，避免引入各自独立入口的循环、重复装载与成像。
            gm = region(sources[gm_file], 60, 68)
            pump = region(sources[pump_file], 209, 217)
            # GM 不再自行 new 一套 MOT；显式绑定它直接写入的器件，频率保留为阶段形参。
            gm_class = """class GrayMolasses(HasEnvironment):
            def build(self, ew_v, offset_v):
                self.setattr_device("zotino0")
                self.setattr_device("AOM_556_3DMOT")
                self.setattr_device("ttl_556_3DMOT_Sh")
                self.ComField_EW_for_556MOT = ew_v
                self.gray_EW_bias_offset = offset_v

            @kernel
            def cool(self, GM_preparation_freq):
        """
            # 光抽运通过 build 注入当次选定的频率/衰减与线圈偏置，pump 沿用旧操作。
            state_class = """class OpticalPumping(HasEnvironment):
            def build(self, ew_v, offset_v, frequency_hz, attenuation_db):
                self.setattr_device("zotino0")
                self.setattr_device("AOM_556_Probe")
                self.ComField_EW_for_556MOT = ew_v
                self.state_preparation_EW_bias_offset = offset_v
                self.f_preparation = frequency_hz
                self.att_preparation = attenuation_db

            @kernel
            def pump(self):
        """
            # 每段以 @kernel 方法写出，AST 语法检查后，用户可打开整个 .py 再审核。
            source = (header + indent(initialize, "        ")
                      + "\n\n    @kernel\n    def read_and_restore(self):\n" + indent(after, "        ")
                      + "\n\n" + gm_class + indent(gm, "        ")
                      + "\n\n" + state_class + indent(pump, "        ") + "\n")
            ast.parse(source)
            target.mkdir(parents=True, exist_ok=True)
            (target / "atom_stage_adapter.py").write_text(source, encoding="utf-8")
            return target / "atom_stage_adapter.py"
    ''',
    )
    add(
        "code",
        r"""
        def source_default(source, name):
            # 仅解析此固定版本实际使用的常量、乘法和单位；不执行文件。
            def scalar(node):
                if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                    return float(node.value)
                if isinstance(node, ast.Name):
                    return {"MHz": 1e6, "ms": 1e-3}[node.id]
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                    return scalar(node.left) * scalar(node.right)
                raise ValueError(f"参数 {name} 的默认表达式需重新审核。")

            matches = [
                n.args[1] for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "setattr_argument" and len(n.args) >= 2
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == name
            ]
            processor, = matches
            if processor.func.id == "NumberValue":
                return scalar(processor.args[0])
            assert processor.func.id == "Scannable"
            defaults = next(k.value for k in processor.keywords if k.arg == "default")
            point = defaults.elts[0]
            assert point.func.id == "NoScan"
            return scalar(point.args[0])


        if ATOM_MASTER is not None:
            lab_values = {
                "ew_v": source_default(atom_sources[base_file], "ComField_EW_for_556MOT"),
                "tof_s": source_default(atom_sources[clock_file], "TOF_time"),
                "gm_frequency_hz": source_default(atom_sources[gm_file], "gray_molasses_preparation_freq"),
                "gm_offset_v": source_default(atom_sources[gm_file], "gray_EW_bias_offset"),
                "pump_offset_v": source_default(atom_sources[pump_file], "state_preparation_EW_bias_offset"),
                "pump_frequency_hz": source_default(atom_sources[pump_file], "f_preparation"),
                "pump_att_db": source_default(atom_sources[pump_file], "att_preparation"),
            }
            adapter_root = OUT / "production_adapter"
            print(write_stage_adapter(atom_sources, adapter_root))
            print("本次离线编排使用的源码默认值：", lab_values)
    """,
    )
    add(
        "markdown",
        """
        ### 按自己的模块替换每项参数

        | 传入的位置 | 本例的实参 | 你在自己工程中要填的内容 |
        |---|---|---|
        | `NativeModule.from_package(root=..., entry=...)` | `production_adapter` 和 `atom_stage_adapter:GrayMolasses` | 前者是 Python 导入根目录，后者是该根下的模块名和类名。例如文件为 `src/my_lab/gm.py` 时写 `root=src`、`entry="my_lab.gm:Cooling"` |
        | `source_overrides` | `lib` 与 `lib._GreenMOTbase` 的工作副本文件 | 将根目录外的原生依赖准确映射到模块；原始基类的外部库需安装在 ARTIQ 运行环境 |
        | `resources` | `AOM_556_3DMOT`、`zotino0`、快门、共用 CPLD | 使用实际 `device_db` 器件名，逐项对照所有可达 kernel；DDS 和开关可能还有同一物理总线 |
        | `build_kwargs` | `ew_v`、`offset_v`、`frequency_hz`、`attenuation_db` 等 | 键要与该类 `build(self, ...)` 的参数名一致；值在实例创建时固定。扫描量放到 stage 的调用处，例见 `GM_cooling(gm_frequency_hz)` |
        | `initialize`、`after_shot`、`finalize` | 本例仅 MOT 选择 `initialize_devices` 与 `read_and_restore` | 分别为会话首次初始化、每个完成 shot 的结尾及会话最终收尾。异常可能中断执行，故障关断由真机入口负责 |
        | `NativeProgram(modules=..., stages=...)` | `"gm" → GrayMolasses`，`"GM_cooling" → NativeStage("gm","cool", ...)` | 给实例取名，再将实验作者要调用的方法名映射到实例的 kernel。入参依照该 kernel 的签名 |
        | `shared_resources` | `"urukul3_cpld": ("mot","gm","state")` 等 | 每一项列出全部参与者。只允许一个参与者做初始化，并让每一步自行建立需要的通道状态 |

        本例 `gm_frequency_hz` 每次作为 `cool(self, GM_preparation_freq)` 的参数传入；
        `gm_offset_v` 则在构造 GM 实例时设置为 `self.gray_EW_bias_offset`。
        用同样的方法，可以把某个原本只在 Dashboard 中调整的量改为 DSL 扫描轴：先给薄适配 kernel
        增加命名清晰的显式形参，再在 `exp.sweep(...)` 里将该 `SweepParameter` 传给对应的 stage。
    """,
    )
    add(
        "code",
        r"""
        from taiyidsl import NativeModule, NativeProgram, NativeStage


        def register_atom_program(master, adapter_root, values, base_source):
            source_overrides = {
                "lib": master / "lib/__init__.py",
                "lib._GreenMOTbase": master / base_file,
            }

            def component(cls, resources, settings, **hooks):
                # root 是生成文件的父目录；entry 用“模块:类”选中此组件的实现。
                # source_overrides 绑定该工作副本的 lib 与基类，工厂为每个源码文件记录哈希。
                # resources 枚举实际设备和共享总线；build_kwargs 将标量送入该类的 build()。
                # hooks 只选取需要的生命周期方法；没有指定的 run/prepare 均不调用。
                return NativeModule.from_package(
                    root=adapter_root,
                    entry=f"atom_stage_adapter:{cls}",
                    source_overrides=source_overrides,
                    resources=resources,
                    build_kwargs=settings,
                    **hooks,
                )

            # 基类 build() 静态绑定的物理器件；core 由根管理，scheduler 是虚拟设备。
            bound = {
                n.args[0].value for n in ast.walk(ast.parse(base_source))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "setattr_device" and isinstance(n.args[0], ast.Constant)
            } - {"core", "scheduler"}
            board = "urukul3_cpld"   # 两个 556 DDS 通道还共享这一 CPLD/SPI
            # MOT 独占初始化权；ew_v 统一覆盖原基类默认偏置；tof_s 在读出前使用。
            mot = component(
                "MOT", sorted(bound | {board}), {"ew_v": values["ew_v"], "tof_s": values["tof_s"]},
                initialize="initialize_devices", after_shot="read_and_restore",
            )
            # GM 只声明自己使用的 556 MOT 输出、快门、Zotino 和共同板卡；不重复初始化。
            gm = component(
                "GrayMolasses", ("zotino0", "AOM_556_3DMOT", "ttl_556_3DMOT_Sh", board),
                {"ew_v": values["ew_v"], "offset_v": values["gm_offset_v"]},
            )
            # 态制备 build 接收抽运光频率/衰减以及偏置增量；源码片段使用这些对象属性。
            state = component(
                "OpticalPumping", ("zotino0", "AOM_556_Probe", board),
                {"ew_v": values["ew_v"], "offset_v": values["pump_offset_v"],
                 "frequency_hz": values["pump_frequency_hz"], "attenuation_db": values["pump_att_db"]},
            )
            # modules 的键是组件实例名。stages 的键是实验作者真正调用的三个 DSL 方法。
            # 每个 NativeStage 依次指定：实例名、那个实例上的 @kernel 方法、时间边界类型。
            return NativeProgram(
                modules={"mot": mot, "gm": gm, "state": state},
                stages={
                    "prepare_MOT": NativeStage("mot", "load", timing="prefix"),
                    "GM_cooling": NativeStage("gm", "cool", timing="prefix"),
                    "state_prep": NativeStage("state", "pump", timing="prefix"),
                },
                # 各设备列出全部使用者。三者共享 CPLD/SPI 和 Zotino；仅 mot 初始化。
                shared_resources={
                    "zotino0": ("mot", "gm", "state"), board: ("mot", "gm", "state"),
                    "AOM_556_3DMOT": ("mot", "gm"), "ttl_556_3DMOT_Sh": ("mot", "gm"),
                    "AOM_556_Probe": ("mot", "state"),
                },
            )


        production_native = None
        if ATOM_MASTER is not None:
            production_native = register_atom_program(
                Path(ATOM_MASTER), adapter_root, lab_values, atom_sources[base_file]
            )
            print(production_native.to_dict()["lifecycle"])
    """,
    )
    add(
        "code",
        """
        def production_shot(q, native, probe, awg_frequency, *, gm_frequency_hz, guard, shots):
            exp = Experiment("atom_control_578_spectroscopy", [q])
            # shots 决定重复数；awg_frequency 是电频率扫描轴；realtime 包含整个 shot。
            with exp.average(shots), exp.sweep(awg_frequency), exp.realtime():
                native.prepare_MOT()
                native.GM_cooling(gm_frequency_hz)  # 此处参数是经核对的 GM DDS 频率（Hz）
                native.state_prep()                # 光抽运时长已保存在原生 kernel 内
                # 外触发启动同一块 AWG；guard 是波形内的零输出时长（非原子端时延）。
                with exp.triggered_sequence(group="clock_awg"):
                    delay(guard)
                    play(q.clock, probe, frequency=awg_frequency)
                # 顶层 kernel 接下来调用 mot.read_and_restore()，发三次相机触发并收尾。
            return exp
    """,
    )
    add(
        "markdown",
        """
        生成的 `production_adapter/atom_stage_adapter.py` 可直接逐行对照上游文件。把它与
        固定的 `lib/__init__.py`、`lib/_GreenMOTbase.py` 和完整导出目录一起交给 ARTIQ 环境；
        worker 上两处模块根必须可导入且与导出的源路径、哈希匹配。迁移目录后应在最终路径重新导出。
        此版本基类在导入时需要 `tifffile`。修改 GUI 入口、源码或接线后，应重新做离线编译。
        基类会初始化 `AOM_578_Shift`，原实验另外驱动 `AOM_578_Clock`。外部 AWG 方案必须核对两者及目标射频链的实际开关/路由。
        本组合仍沿用制备源中的光抽运与读出源中的 TOF。原钟谱的每点 OFF/ON 两次独立装载、
        卫星 DDS 初始化及异常关光逻辑属于另一条运行链。本例生成单脉冲频谱；做配对实验时，
        需显式安排 OFF/ON、每支的装载和相应帧编号，接入本实验台的失败关断策略。
    """,
    )
    return cells
