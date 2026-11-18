# 仓库维护工具

实验函数从 `taiyidsl` 导入，库实现位于 [src/taiyidsl](../src/taiyidsl/)。
这里的程序从源码仓库运行，读取示例、生成教程或检查导出结果。wheel 只安装库；
源码发行包另含这些工具。第一次编写实验可以直接打开 [Plain Ramsey 教程](../docs/tutorials/ramsey_clock_plain.ipynb)。

| 维护任务 | 工具与输入 |
| --- | --- |
| 修改教程 | 四个 `build_*.py` 生成五份教学 notebook。Ramsey 的 plain 版和 operation 版共用一个生成器。每份教程对应的文件与运行命令见[教程维护表](../docs/tutorials/README.md)。 |
| 执行或比较教程 | [execute_hybrid_tutorial.py](execute_hybrid_tutorial.py) 在新 kernel 中执行指定 notebook。[tutorial_semantics.py](tutorial_semantics.py) 比较选定实验的编译结果。教程 CI 直接调用这两个入口。 |
| 复核 ARTIQ 代码 | [compile_native_artiq.py](compile_native_artiq.py) 在独立 ARTIQ 环境中编译已导出的程序。[artiq-python.ps1](artiq-python.ps1) 启动 Windows 工具链解释器。安装与参数见[工具链说明](../docs/artiq_toolchain.md)。 |
| 定位驱动时序差异 | 按导出使用的模式选择 [square](verify_square_driver.py)、[RAM](verify_ram_driver.py)、[polar](verify_polar_driver.py) 或 [mixed polar](verify_mixed_polar_driver.py)。它们用真实驱动代码和虚拟 RTIO 检查数字事件。[audit_phase_paths.py](audit_phase_paths.py) 检查驱动相位路径，[verify_native_preparation_tail.py](verify_native_preparation_tail.py) 检查 Zotino 准备尾段。 |
| 比较排程改动 | [verify_section_plan_baseline.py](verify_section_plan_baseline.py) 从示例编译时序、相位、AWG 样本摘要与 shot 顺序，与指定 JSON 基线比较。 |
| 分析 Spectrum 数据与导出 | [spectrum/](spectrum/) 包含[批量导出示例](spectrum/export_hybrid_examples.py)、[解读已导出的 Ramsey](spectrum/explain_spectrum_ramsey.py)、[比较相位写法](spectrum/verify_spectrum_phase_equivalence.py)和[分析已保存的测量数据](spectrum/analyze_spectrum_calibration.py)。 |
| 盘点待发布文件 | [repository_inventory.py](repository_inventory.py) 按 Git 状态和发布规则生成文件清单。输出与审阅方法见[仓库内容说明](../docs/repository_contents.md)。 |

使用旧版 Spectrum 命令的实验室脚本，将上表四个文件的前缀从 `scripts/` 改为
`scripts/spectrum/` 即可，命令参数和输出路径相同。

需要被多个实验导入的功能应放入所属的库模块；可运行的实验写法放在
[examples/](../examples/)，可重复的行为断言放在 [tests/](../tests/)。
