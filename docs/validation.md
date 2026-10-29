# 首版验证记录

日期：2026-10-29。环境：项目 uv 环境、CPython 3.12.15。没有连接或操作仪器。

## 自动化结果

- **113 项 pytest 测试通过**，无跳过；JUnit 报告：`output/test-results.xml`。
- 根目录 `ruff check .` 与 `ruff format --check .` 通过：88 列，完整启用 `E/F/W/I/UP`。
- Rabi → 校准建议 → 显式持久化应用 → 新参数 Ramsey/RB 的完整示例通过。
- 三个实验均生成了 compiled JSON、独立离线 HTML、ARTIQ 源码/manifest 及结果文件。
- 导出源码通过 Python 语法和独立 fake-driver 事件检查；变异测试能捕获时序、寄存器、目标时钟和配置预算错误。
- 参数化和展开导出逐点等价检查通过，包括实际驱动事件顺序、时间、DDS 寄存器值与相位参考、配置耗时和 shot 结束位置；生成的 host 循环及读出适配器调用顺序也通过检查。
- 离线 HTML 的内嵌脚本通过 Node.js `--check` 语法检查；没有外部 script 加载依赖。
- 初始化示例新增 3 项测试：零抽运点、对照与 push-out 等长读出时序、配置快照，以及隔离运行示例并生成完整离线产物。

## 本地规范整改与行为保持

固定 CPython 3.12.15，保留依赖锁文件；英文 README、中文指南、贡献指南、PR 模板和跨平台换行配置已补齐。源码包明确限制打包范围，不含环境、缓存、运行数据或临时目录。

整改前保存了源码语法树和四组实验的执行事件报告。原有 22 个 Python 文件中，19 个在格式调整后语法树完全一致；其余 3 个仅有 Ruff 的标准库现代化调整：`typing.Mapping` 改为 `collections.abc.Mapping`，`timezone.utc` 改为等价的 `datetime.UTC`。实验控制逻辑没有改写。

重新生成 Rabi、Ramsey、RB 和 initialization 示例后，共 123 个扫描点的事件轨迹和结束时间与整改前一致；32,400 次 host 调度的顺序检查同样保持一致。比较报告：`output/standards_review/behavior_comparison.json`。源码行号及包含来源位置的 provenance hash 会随格式变化，不能把这些元数据差异当成时序变化。

wheel 和 sdist 构建通过，并检查了内容清单；在新的隔离环境中按锁文件安装运行依赖及 wheel，确认包来自 `site-packages`，完成 Rabi 编译与双模式导出等价性检查。记录：`output/standards_review/wheel_smoke.json`。

`.github/workflows/ci.yml` 已配置 Windows/Linux、固定 Python、Node 22、锁文件同步、根目录 lint/format、pytest、构建和示例产物留存；YAML 本地解析通过。**尚未上传仓库，因此远程 GitHub Actions 和 Linux runner 执行未验证。** 本次没有创建远程仓库、提交或上传代码。

示例合成数据的 Rabi π 时长真值为 12 µs，拟合约 12.014 µs。Qubit revision 从 0 更新到 1，旧实验仍保留 10 µs 的原始参数。这证明参数和数据流程，不证明实验室物理精度。

## taiyidsl 命名与迁移验证

发行包、Python 导入、源码目录、文档和示例统一使用 `taiyidsl`；项目环境按原锁定依赖重新创建。
23 个原有 Python 文件仅替换名称后，语法树与迁移后的源码完全一致。
113 项测试通过；四组示例的 123 个扫描点驱动事件哈希、结束时间，以及 32,400 次 host 调度检查均与迁移前一致。
比较记录：`output/rename_validation/behavior_comparison.json`。
新命名的 wheel 在隔离环境安装后通过包元数据、导入、Rabi 编译和两种导出模式等价检查；记录：`output/rename_validation/wheel_smoke.json`。
新目录验证发现 pytest 的嵌套临时路径依赖已有父目录，因此 CI 和复现命令改用可直接创建的 `--basetemp=.test-tmp-pytest`。

## 默认参数化导出与调试展开导出

同一个编译产物分别导出 `mode='auto'` 和 `mode='unrolled'`，重建每个扫描点的驱动事件并与原始 compiled timeline 比对；两种代码的事件再直接比较。以下行数包含整个生成的 Python 文件，kernel 数仅计算 shot 程序，不含共同的设备初始化 kernel。

| 示例 | 扫描点数 | 默认源码行数 / shot kernel 数 | 展开源码行数 / shot kernel 数 | host 顺序检查 |
|---|---:|---:|---:|---|
| Rabi | 49 | 146 / 1 | 1221 / 49 | cyclic，14,700 shots 通过 |
| Ramsey | 41 | 143 / 1 | 1197 / 41 | sequential，12,300 shots 通过 |
| RB | 21 | 1976 / 2 | 3429 / 21 | cyclic，4,200 shots 通过 |

Rabi 的零时长点、Ramsey 的零延迟点通过可选事件槽共享同一个 kernel；RB 中无法合并的事件结构生成两个 kernel。默认优化同样适用于自定义 sweep 实验，内置模板没有专用导出分支。每个实验的 `export_comparison.json` 保存逐点检查和源码统计，`artiq/experiment.py` 为默认版本，`artiq_unrolled/experiment.py` 为调试版本。

## 明确未验证

- **真实 ARTIQ 编译：not_verified**。本机没有安装实际 ARTIQ 编译工具链。
- **实机时序、光学相位、RTIO 吞吐：not_verified**。参考设备时序预算尚未实测。
- **浏览器视觉和交互：未实测**。浏览器工具安全策略禁止打开本地 `file://` 页面，因此没有绕过限制改用其他浏览器或服务。已完成生成结构、内容与脚本语法检查；用户可直接打开 `output/tuneup_workflow/ramsey/timeline.html` 检查实际交互。

RB 的 `p_std_shot_only` 是条件于样本序列的 shot-noise 拟合不确定度，不包含随机序列间方差。原始 sequence 轴保留，后续可增加序列级 bootstrap。

## 重现命令

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
uv sync --locked
uv run --locked pytest --basetemp=.test-tmp-pytest --junitxml=output/test-results.xml
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python examples/tuneup_workflow.py
uv run --locked python examples/state_initialization.py
uv build --no-sources
```

此记录与生成的 `verification_report.json` 分开保存：后者记录每个实验的点数、参数版本、量化诊断、导出检查和拟合结果。
