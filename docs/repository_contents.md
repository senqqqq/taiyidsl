# 仓库内容、安装与发布准备

这是实验室内部协作版本。源码、可复现示例和验证工具进入 Git；依赖环境、编译器、
生成报告和实验数据保留在本地。不要通过删除测试或实验数据来缩小仓库。

## 上传与本地保留

| 内容 | Git / 发行包策略 |
| --- | --- |
| `src/taiyidsl/`，包括 viewer JS | Git、wheel、sdist |
| `tests/`、`tests/data/`、`tests/fixtures/` | Git、sdist；基线是有效测试输入 |
| `examples/`、`scripts/`、`docs/` 与无输出 notebook | Git、sdist；保留教程的全部源码输入 |
| README、CONTRIBUTING、AGENTS、CI、uv 配置与锁文件 | Git、sdist |
| `.venv/`、`.uv-cache/` | 本地依赖与缓存，不上传 |
| `.toolchains/` | 独立 ARTIQ 发行环境与安装器，不上传 |
| `output/` | 报告、bank、原始数据、源码快照和编译证据，不上传 |
| `dist/`、`build/`、pytest/Ruff/Python 缓存 | 构建或临时产物，不上传 |
| `artiq_demo/`、根目录第三方 RB helper | 本地参考资料，不是运行依赖 |
| 本地 MEMORY 和两份根历史计划 | 本地保留；当前约定见[设计决定](design_decisions.md) |
| `.env`、`.env.local`、`local_config/`、根 `dataset_db.mdb` | 本地配置或数据库，不上传 |

忽略规则不代表可以删除。特别是 `output/` 可能含不可再生数据、校准、银行文件、
ELF 和交付依赖；清理必须先检查引用与用途。不使用 `*.json`、`*.db` 等宽泛规则，
以免隐藏合法离线配置和基线。若已跟踪文件后来被忽略，须单独审查 Git index；
`.gitignore` 本身不会让已跟踪文件停止上传。

根目录 `randomized_benchmarking_helper.py` 是未被本项目引用的第三方参考源码，
保留本地版权头，不作为 TaiyiDSL 自有代码分发，也不因此添加 LabOneQ 依赖。
仓库暂未选择开源许可证；内部协作不自动授予对外再分发权利。

## 四层安装边界

从源码 checkout 根目录执行：

```bash
uv sync --locked
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/yb171_global_rf_tuneup.ipynb
```

第一条安装核心与开发依赖；后两条用于教程。Python 由 `.python-version` 固定，
依赖由 `pyproject.toml` 和 `uv.lock` 共同描述，不需要复制现有 `.venv` 或第三方库。
教程使用仓库里的示例、数学辅助与 native fixture，因此从 checkout 运行；仅安装
wheel 是使用库的方式，不包含教程。修改依赖用 `uv add`，同步更新并提交锁文件。

| 层 | 安装方式与边界 |
| --- | --- |
| DSL / notebook | uv 管理的 CPython 3.12；默认无需 ARTIQ、SDK 或仪器 |
| Spectrum Python SDK | `uv sync --locked --extra spectrum`；锁定版本为 1.13.4 |
| ARTIQ 与 Spectrum 原生驱动 | 各自的官方发行环境；见[工具链说明](artiq_toolchain.md) |
| 实验室 native 包与相机 SDK | 在实际执行这些代码的环境安装；见[扩展示例](../examples/lab_extension/README.md) |

SDK 依赖允许范围不等于全部版本已经验证；当前 DDS 行为基于 1.13.4。Python SDK
不代替卡的原生驱动。ARTIQ 的完整编译器也不能用普通 `uv add artiq` 替代。
DSL 与 ARTIQ 使用独立解释器；通过 `LabConfig.artiq_python` 指定实际 ARTIQ Python。
项目内 Windows 工具链位置只是默认路径，不能据此宣称 Linux 实机运行已验收。

## 发布清单与审阅

```bash
uv run --locked python scripts/repository_inventory.py
uv run --locked ruff check .
uv run --locked ruff format --check .
```

盘点命令生成本地 `output/release_prep/file_inventory.csv`（路径、大小、Git 状态、
发布分类、原因）、`publish_files.txt`、`publish_hashes.json` 和
`inventory_summary.json`。它不 stage、删除或上传文件。依赖和工具链逐文件列出，
Git 内部对象和清单自身的输出目录另作说明，不递归跟随目录链接。
扫描只报告可能的凭据、设备地址和大文件的位置，不回显凭据内容；结果需人工审阅，
不能视为完整安全认证。新增文件未跟踪不等于应排除，须以清单和用途决定。

发布前验证 wheel 的运行资源和独立安装，sdist 的文档链接、例子和教程输入。
最终两份重点 notebook 应在新 kernel 离线运行；实验意图与语义保持一致，允许
更新导入和组件用法。具体检查范围见 [CONTRIBUTING](../CONTRIBUTING.md)。

只推送明确选定的分支，不用 `git push --mirror`。本地代理 checkpoint/recovery refs
可能包含大型数据库或历史工作区，不属于发布历史，也不应为发布而删除恢复资料。
远端位置、成员权限和分支保护由组织配置；本文件不授权任何推送。
