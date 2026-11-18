# 最小实验室扩展包

这是可复制成独立仓库的 `src/my_lab` 包，示范通用 DSL 和实验室专用代码的边界。
所有图像都是人工数组；没有厂商 SDK、真实地址、设备连接或原生 kernel 执行。
相机 TTL 源码只是离线组件示例，不代表已经完成真实 ARTIQ 编译或硬件验收。

## 安装与运行

在 TaiyiDSL 源码项目根目录先安装主项目，再显式向同一解释器安装扩展包：

```powershell
uv sync --locked
$labPython = uv run --no-sync python -c "import sys; print(sys.executable)"
uv pip install --python $labPython --no-deps -e examples/lab_extension
uv run --no-sync python -m my_lab.demo
```

Linux/macOS shell 的等价安装命令：

```bash
uv sync --locked
lab_python="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
uv pip install --python "$lab_python" --no-deps -e examples/lab_extension
uv run --no-sync python -m my_lab.demo
```

`--no-deps` 是因为主项目已经按锁文件安装；扩展的项目声明仍记录 `taiyidsl` 依赖。
后续示例用 `--no-sync`，避免主项目同步清除这个未列入主锁文件的本地扩展。
真实实验室包应维护自己的依赖清单和锁文件，使用约定的私有仓库或 wheel 来源。
不要把设备地址、校准或相机 SDK 加进 TaiyiDSL 通用依赖。

结果位于 `output/lab_extension/`。每次创建独立的 `run_*` 目录保存原图、观察提交
与 SQLite journal；`summary.json` 和普通图像归档更新为本次演示结果。
可以用 `--output <directory>` 指定其他输出位置，勿指向实测数据目录。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `src/my_lab/native.py` | ARTIQ 相机 trigger 类，显式 initialize/finalize；不传输图像 |
| `src/my_lab/declarations.py` | `NativeModule.from_package` 固定源码和资源，`NativeProgram` 暴露 suffix stage |
| `src/my_lab/camera.json` | 示例 ROI、背景与分类阈值；不是实际标定 |
| `src/my_lab/configuration.py` | 显式建立 native-triggered 图像结果 schema |
| `src/my_lab/camera.py` | 假相机、普通 adapter 与带恢复能力的持久 adapter |
| `src/my_lab/demo.py` | 可运行的离线 hook 顺序与数据恢复演示 |

导入 `my_lab.declarations` 不会导入 `my_lab.native` 或 ARTIQ。
原生代码部署到独立 ARTIQ 环境；host adapter 留在取数所用的 Python 环境，
各自的第三方依赖分别安装。真实 device_db 必须声明资源 `camera_trigger`。

## 连接到实验

原生声明作为实验函数的显式参数传入，实验正文可以在控制段后调用
`native.readout(10 * us)`。这里 stage 声明的 duration 是固定的 10 µs，调用参数
也必须匹配；更换曝光或触发设置时应相应修改声明并重新编译。相机曝光和传输
不会由这个 TTL 宽度自动推导。其他 native 组件见
[原生组件指南](../../docs/native_components.md)。

普通导出程序设置 `experiment.result_adapter = CameraAdapter(...)`。
持久运行使用
`runner.run(artifact, result_adapter=PersistentCameraAdapter(...), run_store=store)`。
具体路径的回调与索引区别见 [读出契约](../../docs/readout_adapters.md)。

演示中显式写入带 `simulated` 标记的 journal 状态，只为展示恢复边界。真实运行
由 `StreamingRunner` 管理这些状态，adapter 只保存原始帧并返回观察 receipt。
不要复制演示循环代替硬件 runner。`finish` 不是异常清理保证；真实 SDK 连接
需在外层 `try/finally` 或 context manager 中关闭。

## 验证

定向测试覆盖普通两参数回调、empty/missing 区别、先触发后取帧、原始数据已保存
但观察提交中断的恢复，以及恢复不重发硬件 shot。它们是软件与 mock 验证，
不构成真实相机或 ARTIQ 编译验收。
