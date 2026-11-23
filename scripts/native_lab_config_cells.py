"""Chinese notebook cells for resolving an atom-control laboratory configuration."""

from textwrap import dedent


def config_cells():
    cells = [
        (
            "markdown",
            """
            ## 把实验台的设备名称接到混合后端

            **先画出两根线。** 一根是 Kasli 的某路空闲 TTL → Spectrum 的外触发输入。
            另一根是 Spectrum 的某路 DAC → 实验室实际用于 578 nm 探测的射频链。
            确认卡号、输出端口、触发极性、参考时钟、射频开关和光学通路，再把它们写入接线描述。
            ARTIQ 的 `device_db` 定义 core、TTL、DDS、Zotino 和别名。
            Spectrum 卡的身份与设置放在 `spec_db`。
            `descriptor` 决定逻辑信号 `q.clock` 最终送到哪个物理端口。

            所引生产版本把 `urukul6_ch3` 指向 `AOM_578_Clock`，把 `urukul0_ch3` 指向
            `AOM_578_Shift`。前者是原生钟谱程序使用的卫星 DDS。
            混合示例选择 Spectrum 作为钟脉冲波形源，需要核对同轴接线。
            原来的钟 DDS 如保留作本振或固定移频，
            要单独约定它的工作点、开关及归属，避免两条控制路径同时驱动同一输入。数据库中的
            `ttl6` 是 `ttl_Camera_Trig` 的别名。`ttl_RF_Gate_Trig` 已在原程序中使用。
            两者都不能仅凭名字选作空闲的 AWG 触发线。
            [原始设备库](https://github.com/TaiyiQ/atom-control/blob/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/device_db.py)

            下面的函数在本机解析文件。先用教学卡 B、DAC 0 和教学 TTL 演练。
            查看 `connections` 和 `trigger_connections`，
            分别按“信号 → 端口”和“TTL → 卡”对照接线。
            """,
        ),
        (
            "code",
            """
            from taiyidsl import DeviceSetup


            def build_clock_setup(
                device_db, spec_db, *, card_id, clock_port, trigger_name
            ):
                descriptor = {
                    "name": "clock_spectroscopy",
                    "backend": "artiq-spectrum",
                    "instruments": {
                        "artiq": {"kind": "artiq", "device": "core"},
                        card_id: {"kind": "spectrum", "device": card_id},
                    },
                    "channels": {
                        "clock_drive": {"instrument": card_id, "channel": clock_port},
                    },
                    "signals": {"q": {"clock": "clock_drive"}},
                    "sync_groups": {
                        "clock_awg": {"cards": [card_id], "trigger": trigger_name},
                    },
                }
                return DeviceSetup.from_descriptor(
                    descriptor, device_db=device_db, spec_db=spec_db
                )


            example_dbs = ROOT / "examples" / "artiq_spectrum" / "ramsey_lab"
            wiring_example = build_clock_setup(
                example_dbs / "device_db.py", example_dbs / "spec_db.py",
                card_id="B", clock_port=0, trigger_name="awg_trigger_b",
            )
            print("逻辑信号和物理端口", wiring_example.connections())
            print("触发输出和目标卡", wiring_example.trigger_connections())
            """,
        ),
        (
            "markdown",
            r"""
            ### 将生产 `device_db.py` 变为可静态检查的快照

            生产文件包含 `import os`、`ORCA_*` 环境变量、逐项写入和卫星编号等动态设置。
            TaiyiDSL 的文件读取器只处理声明式字典。**在运行原项目的可信配置环境中**，
            核对 checkout、ORCA 环境与 master 一致后，用 PowerShell 读取设备定义。
            该命令会执行你指定的 Python 配置文件，应先审查本地版本。
            用实际路径替换示例路径。输出到实验室私有目录，
            保留整数卫星编号和别名，并在首行记录原始文件哈希。

            ```powershell
            $db = 'C:/lab/atom-control/artiq-master/device_db.py'
            $snapshot = 'C:/lab/private_config/device_db_snapshot.py'
            @'
            import hashlib
            import pprint
            import runpy
            import sys
            from pathlib import Path

            source, output = map(Path, sys.argv[1:])
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            devices = runpy.run_path(str(source))["device_db"]
            body = pprint.pformat(devices, sort_dicts=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                f"# source_sha256={digest}\ndevice_db = {body}\n", encoding="utf-8"
            )
            '@ | python - $db $snapshot
            ```

            在同一私有目录准备 `spec_db.py`，按教学文件填写实测卡的序列号、型号、
            参考时钟、采样率、启用通道、输出量程和触发延迟；另建 `clock_route.toml`，
            填写 `card_id`（与 `spec_db` 的键一致）、`clock_port`（整数）和
            `trigger_name`（经现场确认空闲、已接外触发的设备名）。
            本例的混合后端支持 M4i.6631-x8、1.25 GS/s 与 125 MHz 参考。
            触发线以外，三段原生代码使用的 DDS、共享 CPLD/SPI、DAC、相机等也必须由同一份
            设备库解析。它们由原生模块声明。
            专用于原生 kernel 的设备通常不用逐个加到 DSL 的 `channels`。
            配置快照含实验室网络与仪器身份，只在实验室保存；环境变量改变时要重新生成。
            [设备描述与同步校准指南](../device_setup_descriptor.md)
            """,
        ),
        (
            "code",
            """
            from hashlib import sha256
            from pathlib import Path
            import tomllib

            # 可选。填好 ATOM_MASTER 后，再将下一行换成本机保存三份文件的目录。
            PRIVATE_CONFIG = None  # 例如 Path("C:/lab/private_config")
            reviewed_clock_setup = None
            if PRIVATE_CONFIG is not None:
                if ATOM_MASTER is None:
                    raise ValueError("ATOM_MASTER 应指向含 device_db.py 的目录")
                private_dir = Path(PRIVATE_CONFIG)
                original_db = ATOM_MASTER / "device_db.py"
                snapshot = private_dir / "device_db_snapshot.py"
                original_hash = sha256(original_db.read_bytes()).hexdigest()
                with snapshot.open(encoding="utf-8") as stream:
                    if stream.readline().strip() != "# source_sha256=" + original_hash:
                        raise ValueError("设备库来源不符，请重新核对并导出快照")
                with (private_dir / "clock_route.toml").open("rb") as stream:
                    route = tomllib.load(stream)
                reviewed_clock_setup = build_clock_setup(
                    snapshot, private_dir / "spec_db.py", **route
                )
                print("设备库快照 SHA-256", sha256(snapshot.read_bytes()).hexdigest())
                print("核对现场接线", reviewed_clock_setup.connections())
                print("核对现场触发", reviewed_clock_setup.trigger_connections())
            """,
        ),
        (
            "markdown",
            """
            ### `experiment_config` 给谁读？从调用者追到字段

            在所引版本，三个 20261120 实验及其父类 `lib/_GreenMOTbase.py` 通过
            `setattr_argument`、`prepare()` 和代码常量组织参数；
            这些程序没有直接加载六个 TOML。
            例如，`GrayMolasses.py` 的 `gray_molasses_preparation_freq` 来自 ARTIQ 的
            `Scannable`，继承的 `T_compress` 来自父类 `build()`。保存本次提交所选的
            具体值，确认扫频单位是 Hz、持续时间是秒后，再交给适配层。
            `repository/helpers/mot_3d_config.py` 的 `prepare_mot(exp, config_dir)`
            会读取 `laser_alignment_config.toml`、`coil_config.toml`、
            `MOT_3D_config.toml`，定位 AOM 并计算该版 3D MOT 的参数。
            这是另一条可以独立复用的准备路径。
            [配置目录](https://github.com/TaiyiQ/atom-control/tree/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/experiment_config)
            [读取三个配置的函数](https://github.com/TaiyiQ/atom-control/blob/e2eedceeae6408bfb1c8f6aa7c4da3dfc4184d2e/artiq-master/repository/helpers/mot_3d_config.py)

            | 文件 | 在这个版本中的内容与用途 |
            | --- | --- |
            | `MOT_3D_config.toml` | 399 装载、交接和 556 捕获、冷却、压缩。 |
            | `coil_config.toml` | 线圈设定电压；换算电流须用实际驱动器系数。 |
            | `laser_alignment_config.toml` | 各 Urukul 射频工作点与通道用途。 |
            | `laser_scan_config.toml` | 激光扫描规则，固定工作点取自 alignment 配置。 |
            | `bias_magnetic_field_scan_config.toml` | Zotino 三轴偏磁扫描。 |
            | `absorption_imaging_config.toml` | 此版本为空，没有相机参数。 |

            下格只列出本地配置的顶层键、字节数和哈希，用来检查来源及版本差异。确定需要复用某个
            配置后，在 host 端读取具体键并按文件的单位转换。示例将另一条 3D MOT 路径的
            `coil_ramp_ms` 转成秒并验证数值；这些值不会自动注入下文的 GM 或 state prep。
            """,
        ),
        (
            "code",
            """
            if ATOM_MASTER is not None:
                config_dir = ATOM_MASTER / "experiment_config"
                for config_path in sorted(config_dir.glob("*.toml")):
                    raw = config_path.read_bytes()
                    sections = tomllib.loads(raw.decode("utf-8-sig"))
                    digest = sha256(raw).hexdigest()
                    print(config_path.name, len(raw), digest, list(sections))
                with (config_dir / "MOT_3D_config.toml").open("rb") as stream:
                    mot_config = tomllib.load(stream)
                coil_ramp_s = float(mot_config["sequence"]["coil_ramp_ms"]) * 1e-3
                assert coil_ramp_s > 0
                print("已验证 coil_ramp_ms 的单位转换（仅检查另一条 3D MOT 准备路径）")
            """,
        ),
    ]
    return [(kind, dedent(source).strip()) for kind, source in cells]
