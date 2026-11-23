"""Build the Chinese native ARTIQ integration notebook with stable cell IDs."""

# Preserve Markdown table rows and notebook teaching examples in the generator.
# ruff: noqa: E501
import hashlib
from pathlib import Path
from textwrap import dedent

import nbformat
from native_lab_adapter_cells import adapter_cells
from native_lab_config_cells import config_cells
from native_lab_tutorial_cells import demo_cells
from tutorial_notebook_format import write_formatted_notebook

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "docs/tutorials/native_mot_clock_zh.ipynb"
REVISION = "e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e"
UPSTREAM = f"https://github.com/TaiyiQ/atom-control/blob/{REVISION}/artiq-master"
ENTRIES = f"{UPSTREAM}/repository/20261120"


def introduction():
    return [
        (
            "markdown",
            """
            # 用原生 ARTIQ 准备原子，用 Spectrum 做 578 nm 钟谱

            **一个频点的一次 shot**：装载 Green MOT → 灰色光学黏胶冷却（GM）→
            态制备（本课选 556 nm 光抽运）→ 578 nm 探测脉冲 → 399 nm 成像读出。
            ARTIQ core 执行原生 kernel、安排触发，Spectrum AWG 产生探测链路的电波形。
            上位机在 shot 外准备波形、接收图像，频率扫描的每次重复都重新装载原子。

            **第一次阅读**：在 TaiyiDSL 源码目录运行
            `uv sync --locked --group notebooks`。用 VS Code 打开本文件，选择项目
            `.venv` 的 Python 3.12 内核，再选择「重启内核 → 全部运行」。
            默认单元使用随仓库附带的 ARTIQ 教学类，可直接看到导出的 kernel、
            脉冲表和各频点的最终 DAC 样本，不需要登录 GitHub 或接入仪器。

            **接入自己的工程**：先读下方的源码对照表。在配置单元填入本地
            `atom-control/artiq-master` 路径，再执行设备库和生产适配章节。
            输入文件保留在原 MOT 工程，导出和验证材料集中在
            `output/native_lab_tutorial/`。建议第一次在离线 checkout 上练习，
            原实验仍可在自己的 VS Code 窗口调试。
            """,
        ),
        (
            "code",
            """
            import os
            import sys
            from pathlib import Path

            ROOT = next(p for p in (Path.cwd(), *Path.cwd().parents)
                        if (p / "src/taiyidsl").is_dir() and (p / "examples").is_dir())
            os.chdir(ROOT)
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            OUT = ROOT / "output/native_lab_tutorial"
            OUT.mkdir(parents=True, exist_ok=True)

            # 可直接改为 Path(r"D:\\lab\\atom-control\\artiq-master")。
            # 也可在启动 Jupyter 之前设置环境变量 TAIYI_ATOM_MASTER。
            atom_path = os.environ.get("TAIYI_ATOM_MASTER")
            ATOM_MASTER = Path(atom_path).expanduser().resolve(strict=True) if atom_path else None
            if ATOM_MASTER is not None and not (ATOM_MASTER / "device_db.py").is_file():
                raise ValueError("请选择同时包含 device_db.py、lib 和 repository 的 artiq-master 目录")
            print("Python", sys.version.split()[0], "| 教程输出", OUT)
            print("生产源码", ATOM_MASTER or "未指定，本次运行教学实例")
            """,
        ),
        (
            "markdown",
            f"""
            ## 先确认要复用的是哪一段

            下表按 `production` 的固定提交 `{REVISION}` 阅读。实验台若有本地修改，
            先对照实际文件，再选阶段。特别留意两份不同的文件都定义了 `StatePrep`。

            | 参考文件 | 它在这一版实际做什么 | 放入组合 shot 的部分 |
            |---|---|---|
            | [GreenMOT.py]({ENTRIES}/GreenMOT.py) 和 [基类]({UPSTREAM}/lib/_GreenMOTbase.py) | 入口负责初始化、扫描、可选成像和收尾；装载、压缩与关场在基类 | 复用 `prepare_GreenMOT()` 及调用链，单独安排初始化和逐 shot 收尾 |
            | [GrayMolasses.py]({ENTRIES}/GrayMolasses.py#L52-L76) | `StatePrep.run()` 在 MOT 后设置 556 DDS、偏置场、开关光并恢复偏置，随后飞行成像 | 从内层循环提取 GM 段，保留两端的场稳定等待；MOT 和成像各执行一次 |
            | [Stateprepartion.py]({ENTRIES}/Stateprepartion.py#L138-L194) | `_sequence()` 扫描钟 AOM；`_shot()` 明确跳过 556 光抽运、RF 门和 blow-away | 用来对照 OFF/ON、偏置场、钟光门和 399 三帧读出 |
            | [Stateprepartion_Spectrum.py]({ENTRIES}/Stateprepartion_Spectrum.py#L208-L231) | 先用 556 光抽运，再触发 Spectrum 的 RF 门，随后做 blow-away | 本课只选光抽运作为 state prep。若实验定义还要求 RF 转移、清除未制备原子，请把这两步及其资源、时序另行纳入 |

            实验台原 578 光路使用 `AOM_578_Clock`（卫星 Urukul 的 `urukul6_ch3`）。
            `AOM_578_Shift` 是另一设备。上述 Spectrum 脚本则用卡输出 RF 自旋门。
            本课选择让 **Spectrum 的模拟输出经实验室已标定的 RF/IF 链路驱动 578 探测**。
            如果原接线仍由 Urukul 独占钟 AOM，先保留教学模式；仅改信号名称不能改变物理路由。
            """,
        ),
    ]


def production_export():
    return [
        (
            "markdown",
            """
            ### 用自己的适配层编译一份完整的混合导出

            下一格有两种用法。只提供 `ATOM_MASTER` 时，用真实原生源码配合教学卡和
            断开硬件的原生设备表，验证组合能否编译。再提供私有接线配置时，改为在
            `reviewed_clock_setup` 上离线编译同一实验。两种用法都沿用教学频轴与探测幅度，
            都需要在上机前填写实测的探测中心、单位换算和光学标定。

            这里另存一份仅供编译器实例化驱动的 JSON：在仓库原有的合成 GreenMOT 表中，
            按本版基类更新同一板卡位置的 759 路设备名，再给新快门分配一个教学 TTL。
            这些通道号只属于断开硬件的编译测试，实验室接线由前面的私有快照和 descriptor 提供。
            """,
        ),
        (
            "code",
            """
            if production_native is not None:
                old_fixture = native_root / "greenmot_native/device_db_offline.json"
                compile_devices = json.loads(old_fixture.read_text(encoding="utf-8"))
                compile_devices["AOM_759_Switch"] = compile_devices.pop("AOM_532_Switch")
                free_channel = 1 + max(
                    row["arguments"]["channel"] for row in compile_devices.values()
                    if isinstance(row, dict) and "channel" in row.get("arguments", {})
                )
                compile_devices["ttl_556_Probe_Sh"] = {
                    "type": "local", "module": "artiq.coredevice.ttl", "class": "TTLOut",
                    "arguments": {"channel": free_channel},
                }
                extra_db_path = OUT / "production_compile_devices.json"
                extra_db_path.write_text(json.dumps(compile_devices, indent=2), encoding="utf-8")

                if reviewed_clock_setup is None:
                    # 只选 core 与演示触发线；原生快门和相机全部来自 GreenMOT 合成表。
                    clock_db = {name: example_db[name] for name in ("core", "awg_trigger_b")}
                    assert clock_db.keys().isdisjoint(compile_devices)
                    production_setup = build_clock_setup(
                        clock_db | compile_devices, fixture_cards,
                        card_id="B", clock_port=0, trigger_name="awg_trigger_b",
                    )
                    print("原生输入：生产源码；接线：合成设备和教学 Spectrum 卡")
                else:
                    production_setup = reviewed_clock_setup
                    print("原生输入：生产源码；接线：已选实验室私有快照")
                # emulation=True 只编译；setup 与 q 的信号映射必须一致。
                production_lab = LabConfig(setup=production_setup, qubits=[q], emulation=True)
                production_exp = production_shot(
                    q, production_native, probe, frequency_axis,
                    gm_frequency_hz=lab_values["gm_frequency_hz"], guard=10 * us, shots=2,
                )
                production_compiled = production_lab.compile(production_exp)
                production_dir = OUT / "production_export"
                production_compiled.export(production_dir)
                plan = json.loads((production_dir / "artiq/manifest.json").read_text("utf-8"))
                native_plan = plan["native_composition"]
                assert native_plan["execution"] == "one_top_level_kernel_per_shot"
                assert len(native_plan["points"]) == len(drive_hz)
                print("ARTIQ 点 kernel 数", len(native_plan["points"]))
                print("完整混合导出", production_dir)
            """,
        ),
        (
            "markdown",
            r"""
            **用独立的 ARTIQ 9 解释器复核真实调用链。** 原生适配包导入上游
            `lib._GreenMOTbase`，该文件在模块顶层导入 `tifffile`。令
            `$atomMaster` 指向你刚才读取的同一个 `artiq-master`，确认 ARTIQ
            解释器可以导入 `tifffile`。若实验室单独存放了这项依赖，也可把其
            包的父目录作为另一个 `--module-root`。在 TaiyiDSL 的 PowerShell 中执行：

            ```powershell
            $atomMaster = 'C:/lab/atom-control/artiq-master'
            .\scripts\artiq-python.ps1 scripts/compile_native_artiq.py output/native_lab_tutorial/production_export/artiq `
                --output output/native_lab_tutorial/production_compile --target rv32g `
                --extra-device-db output/native_lab_tutorial/production_compile_devices.json `
                --module-root output/native_lab_tutorial/production_adapter --module-root $atomMaster
            ```
            只把目标 CPU、驱动版本、输出代码和源指纹均对应本次实验的回执用于判断兼容性。
            断开编译时使用模拟身份的本地驱动，现场还要核对卫星、板卡版本、实际延迟及采集路径。
            """,
        ),
    ]


def commissioning():
    return [
        (
            "markdown",
            r"""
            ## 怎样判断已经接通了哪一层

            1. **实验编排**。先看导出清单的 `native_composition`，再读
               `artiq/experiment.py` 中的 `point_0` 及其调用的方法。确认每个 shot
               只装载一次，光抽运之后才有 AWG 触发，399 成像和收尾都只执行一次。
               查验 manifest 中的时基与源文件 SHA，保留匹配的 Python 包和波形文件。
            2. **断开硬件的 ARTIQ 编译**。在已有项目 ARTIQ 9 工具链的 Windows
               checkout 运行下列命令。输入是上一节教学导出的 **ARTIQ 子目录**。
               `rv32g` 是本教程的离线目标；现场选择与 core 匹配的 target。

               ```powershell
               .\scripts\artiq-python.ps1 scripts/compile_native_artiq.py output/native_lab_tutorial/export/artiq `
                   --output output/native_lab_tutorial/fixture_compile --target rv32g `
                   --extra-device-db examples/artiq_spectrum/native_stages/device_db_offline.json --module-root examples/artiq_spectrum
               ```

               查看 `compile_report.json` 中的 `status`、`source_sha256`、每个 kernel 和 ELF。
               该命令使用 `core.host=None` 和合成设备表。得到 ELF 说明选定驱动与目标
               可完成代码生成；Spectrum 的实际触发延迟、RF 幅度和原子响应要分别测量。
            3. **上机前的交接**。实验室入口应装入完整混合导出目录，通过匹配的
               `SpectrumRunner` 配置卡、上传并等待 ready，然后调用 ARTIQ 点 kernel。
               若通过 Dashboard，与 MOT 使用同一 master 的同一 pipeline。
               若使用 `lab.connect()`，它会启动独立进程直连设备，先让原实验释放
               core、卡和相机。保留实际的时钟会话、触发接线、现场标定与版本记录。
            4. **读出钟谱**。用宿主 `result_adapter.before_shot` 使相机就绪，
               `after_shot` 按 point、shot、分支和帧序号关联三张图（原子＋光、光、暗场）。
               原脚本每个频点先做 OFF 参考，再重新装载做 ON。教学扫描只演示频率轴；
               加入参考时两分支保持相同装载、抽运、偏置和探测窗口，仅切换钟光。
               保存原始帧和缺帧状态，再用实验室标定的态选择读出估计激发分数。
               普通吸收图中的原子数下降还可能来自损失。直连 `HardwareSession.run()`
               没有 detector adapter 注入接口；需要帧时使用支持该读出适配器的宿主入口。
            """,
        ),
        (
            "markdown",
            """
            ### 常见故障该查哪一处

            | 看到的现象 | 检查位置和修正 |
            |---|---|
            | 找不到原生类、哈希改变或导入了另一版本 | 核对 `root` 是否指向顶层包的父目录、`entry` 的类名和 ARTIQ worker 的 import 路径。编辑上游文件后重新审查并导出 |
            | 组件初始化冲突、共享别名或触发冲突 | 在实际设备库中将别名追到 DDS 通道、同板 CPLD/SPI 或 TTL。由一个组件初始化共享资源，给 Spectrum 选独占触发端口 |
            | strict stage 中出现 `break_realtime`、RPC 或阻塞等待 | 沿调用链定位；把可变长准备放在 prefix，把 RPC 和数据传输放回宿主。若需要固定末边沿，另拆出可核对时长的 timed 尾段 |
            | 触发早于准备结束或首个脉冲被截断 | 比对末边沿、同步组触发提前量和实测延迟；确认卡 ready，并在实验时序中留出所需间隔 |
            | 生成了扫描点，示波器波形或原子图却完全相同 | 先比较各点最终 DAC 样本，再查 EXT0、模拟输出与 AOM 路由。最后对齐相机帧、OFF/ON 分支和频点索引 |

            更换任何一个真实组件时，先单独复核它的输入、结束后的设备状态和时序边界，
            再重新编译整条 shot。现场请优先比较原程序与组合程序在同一观测点的边沿；
            尤其是 GM 末尾的偏置、光抽运关断、578 光脉冲及 399 相机首个下降沿。

            [原生组件接口](../native_components.md) · [设备库与接线](../device_setup_descriptor.md) ·
            [ARTIQ 工具链](../artiq_toolchain.md) · [相机读出接口](../readout_adapters.md)
            """,
        ),
    ]


def build():
    parts = [
        *introduction(),
        *config_cells(),
        *demo_cells(),
        *adapter_cells(),
        *production_export(),
        *commissioning(),
    ]
    cells = []
    for kind, source in parts:
        source = dedent(source).strip()
        cell = (
            nbformat.v4.new_code_cell(source)
            if kind == "code"
            else nbformat.v4.new_markdown_cell(source)
        )
        cell.id = hashlib.sha256(source.encode()).hexdigest()[:16]
        cells.append(cell)
    notebook = nbformat.v4.new_notebook(cells=cells)
    notebook.metadata.kernelspec = {
        "display_name": "Python 3 (TaiyiDSL)",
        "language": "python",
        "name": "python3",
    }
    notebook.metadata.language_info = {"name": "python", "version": "3.12"}
    write_formatted_notebook(notebook, NOTEBOOK)
    print(NOTEBOOK)


if __name__ == "__main__":
    build()
