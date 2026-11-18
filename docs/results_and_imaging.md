# 实验结果、完整上下文与离线图像分析

`ResultSet` 保存逐 shot 标量结果；`ImageResults` 在其上保存固定光镊阵列的相机原始帧、ROI 积分和占据判别。两者都不连接仪器。完整可运行示例见 [offline_imaging_workflow.py](../examples/offline_imaging_workflow.py)：

```powershell
uv run --locked python examples/offline_imaging_workflow.py
```

示例扫描 flat-top 时长，使用人工图像验证数据链，输出 HDF5、统计图、时序图和离线 ARTIQ 导出。人工图像不是原子动力学模拟或实测结果。

## 保存哪些说明性信息

使用 `ResultSet.from_compiled(compiled, metadata=...)` 或 `ImageResults.from_compiled(...)` 时，结果自动捕获编译时有效上下文。`result.context` 返回独立副本，修改它不会改动内部快照。

| context 键 | 内容 |
|---|---|
| `lab_config` | 编译时设备拓扑、逻辑信号映射、触发序列、设备标定和后端名称 |
| `optics_config` | schema-v2 光学参数表；没有使用该表时为 `None` |
| `qubits` | 每个 qubit 的参数和版本快照 |
| `experiment` | 实验名称、metadata、参数、qubits 和结构化 IR |
| `compiled_experiment` | 完整编译产物，含扫描轴、逐点逻辑/物理事件、参数、诊断和 provenance |

`context["parameters"]` 同时保留通用参数表。真实 `CompiledExperiment` 的完整快照带 `complete=True`。直接构造 `ResultSet(...)` 不会自动找到实验；轻量合成测试对象缺少编译产物时，`complete=False`。

实验目的、说明和读出约定可放进 `Experiment(..., metadata={...})`；本次采集的备注可通过结果构造函数的 `metadata={...}` 加入。使用可 JSON 序列化的数据，例如文本、数字、列表和字典。原始实验说明保存在 context 中，本次备注保存在 `ResultSet.metadata` 或 `ImageResults.results.metadata` 中。

这些是**序列化数据与 IR 快照**，不会 pickle 活的 Python 对象，也不会自动重建可执行的实验函数。实验 Python 源文件、外部相机 SDK 和外部 asset 引用的文件内容不会自动打包；`ImageResults` 录入的原始帧则嵌入其 HDF5 存档。

## 一个相机帧对应多个 site

下面是可独立运行的离线示例。`acquire` 声明一次触发和对应的 site 集合。`duration` 是触发脉宽；曝光时间放入 metadata 只做记录，不会自动配置相机或插入等待。图像照明和实际装载不由此例提供。

```python
from pathlib import Path

import numpy as np

from taiyidsl import (
    Experiment,
    ImageResults,
    Qubit,
    SweepParameter,
    acquire,
    delay,
    reference_lab,
    us,
)

qubits = [Qubit("q0"), Qubit("q1")]
exp = Experiment(
    "camera_example",
    qubits,
    metadata={"description": "Offline occupancy analysis", "synthetic": True},
)
hold = SweepParameter("hold_time", [2, 4], unit="us")
with exp.average(3):
    with exp.sweep(hold):
        with exp.realtime():
            acquire(
                "q0.readout",
                duration=10 * us,
                handle="pre",
                qubits=qubits,
                metadata={"detector": "camera0", "exposure_s": 100e-6},
            )
            delay("q0.readout", 100 * us)
            delay("q0.raman", hold)
            acquire(
                "q0.readout",
                duration=10 * us,
                handle="post",
                qubits=qubits,
                metadata={"detector": "camera0", "exposure_s": 100e-6},
            )
            delay("q0.readout", 100 * us)
compiled = reference_lab(qubits).compile(exp)

images = ImageResults.from_compiled(
    compiled,
    acquisitions={"pre": {"camera": "camera0"}, "post": {"camera": "camera0"}},
    rois={"q0": (0, 1, 0, 1), "q1": (0, 1, 1, 2)},
    thresholds={"q0": 5, "q1": 5},
    background=1,
    classifier_version="threshold-v1",
    metadata={"description": "Artificial test frames", "synthetic": True},
)
for point, shot in compiled.execution_order():
    images.record_frame("pre", point, shot, np.array([[9, 9]], dtype=np.uint16))
    if shot == 2:
        images.record_frame("post", point, shot, status="missing")
    else:
        frame = np.array([[9, 9 if shot == 0 else 1]], dtype=np.uint16)
        images.record_frame("post", point, shot, frame)

output = Path("output/camera_example")
archive = images.save_hdf5(output / "results.h5")
restored = ImageResults.load_hdf5(archive)
assert restored.context["experiment"]["name"] == "camera_example"
```

ROI 为 `(y_start, y_stop, x_start, x_stop)`，stop 不包含在区域内。背景可以是每像素标量，也可以是与帧同形状的二维背景图。算法先转换为浮点并扣背景，再积分；`counts >= threshold` 判为有原子。ROI、阈值、背景和分类器版本随存档保存。

一张原始整数帧只保存一次，键为 `(acquisition, point, shot)`，可从 `images.frames` 读取。多个 site 在同一帧上独立积分。可传 `timestamp=...` 保存秒数，并在 acquisition 说明中写清时钟来源。不同 acquisition 可以有不同图像尺寸。录入接口接受已经读取的 NumPy 数组；例如外部 NPY 文件可先用 `np.load(path, allow_pickle=False)` 读取。

`ImageResults.from_compiled` 会逐扫描点检查：每个选中的 handle 必须只对应一个逻辑采集事件。
一次 `acquire(..., qubits=[q0, q1])` 可以对应一帧里的两个 site；分别调用两次
`acquire` 或 `measure` 则是两个独立事件，即便时间相同，也不能共用一个图像 handle。
否则构造图像结果时立即报错，避免把一帧误记成两个采集或让后一帧覆盖前一帧。
这不改变 `ResultSet` 对标量观测的 `(handle, qubit)` 索引语义。
独立触发使用不同 handle；若不同 handle 的目标 site 集合不同，分别用相应
`acquisitions` 和 `rois` 构造 `ImageResults`。同一结果对象的所有图像 handle
仍须覆盖其全部 ROI。重复调用同一帧键的 `record_frame` 仍表示显式替换该帧，
不能用来录入另一采集事件。

## xarray：切片、平均和画图

接着上面的例子运行：

```python
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ds = restored.to_xarray()
post = ds.sel(acquisition="post", site="q0")
first_points = post.isel(point=slice(0, 2))
counts = first_points.roi_counts.where(first_points.valid)
counts.mean(dim="shot").plot(x="hold_time")
plt.savefig(output / "mean_counts.png", dpi=150)
plt.close()

summary = restored.survival(pre="pre", post="post")
print(summary.sel(site="q1")["survival"].values)  # [0.5, 0.5]
print(summary.sel(site="q1")["valid_counts"].values)  # [2, 2]
```

`ImageResults.to_xarray()` 返回 `roi_counts / occupancy / status / valid`，维度为 `acquisition, point, shot, site`；另有不带 site 维度的 `frame_valid` 与 `timestamp`。原始图像不填充成零像素矩阵，而是保存在 `frames` 和 HDF5 中。

扫描坐标使用 SI 单位，作为 `point` 上的坐标保留；单位存于坐标的 `attrs["units"]`。例如 `hold_time` 的数值为秒。`point` 保留稳定整数索引，重复扫描值不会合并。按扫描数值筛选可以使用 `ds.where(ds.hold_time <= 3e-6, drop=True)`。视图中的数组与原结果独立；修改 xarray 视图不会改写档案。

标量结果的相应接口是 `ResultSet.to_xarray()`，变量为 `value / status / valid`，维度为 `handle, point, shot, qubit`。使用 `value.where(valid).mean("shot")`，或者现有 `aggregate()` 同时获取均值和有效样本数。不要平均状态字符串。

## 空位、缺失和统计分母

- `occupancy=0, status="valid"`：成功获取图像并判为无原子，是有效观测。
- `status="missing"` 或 `"failed"`：采集不可用，occupancy 和 ROI counts 为 NaN。
- `status="unexecuted"`：尚未执行该采集。
- 初始图像的空位可以用于装载筛选；不能把末次图像空位自动当作采集失败排除。

`survival()` 的分母为“初始有原子，且前后两次采集都有效”的样本数；分子再要求末次有原子。它返回 `survival / valid_counts / survivors / standard_error`。零有效样本时概率为 NaN；误差是二项分布 plug-in 标准误，不是置信区间或读出误差校正。原始 shot 始终保留。

在 push-out 读出中，末次空位可能由选择性移除产生。occupancy/survival 本身不等于核自旋态，需结合读出方法、参考实验和标定另行解释。

## 文件与兼容性

`save_hdf5()` 先写临时文件，完成 flush、close 和 fsync 后替换目标；写入失败不会发布半成品。图片存档主要结构为：

```text
/manifest                   JSON：维度、备注、provenance、完整 context
/observations/<index>/data  逐 shot 标量；图像存档中为 occupancy
/observations/<index>/status
/observations/<index>/roi_counts   图像存档专用
/imaging_settings           acquisition 说明、ROI、阈值、分类器版本
/background
/frames/<index>             原始整数图像；key 属性关联 acquisition/point/shot
/timestamps
```

这是 **TaiyiDSL 自定义 HDF5 布局，不是 NetCDF 文件**。通过 `ResultSet.load_hdf5()` / `ImageResults.load_hdf5()` 读取，再调用 `to_xarray()`；不要把 `xr.open_dataset()` 当作本格式加载入口。图像加载还会验证原始帧、ROI、阈值与已保存分析结果一致。

原有 `ResultSet.save(directory)` 的 NPZ＋JSON 格式继续可用；新写入 schema-v2 增加 context，`ResultSet.load(directory)` 仍接受 schema-v1。HDF5 和旧目录格式使用不同的显式加载方法。

参考设计来源：[labscript/lyse 的 shot 文件与分析结果](https://docs.labscriptsuite.org/projects/lyse/en/stable/api/_autosummary/lyse.Run/)、[ARTIQ datasets 与运行归档](https://m-labs.hk/artiq/manual/using_data_interfaces.html)、[xarray 索引](https://docs.xarray.dev/en/stable/user-guide/indexing.html)与 [I/O 模型](https://docs.xarray.dev/en/stable/user-guide/io.html)。这些来源提供数据组织参考，TaiyiDSL 没有宣称与其文件 schema 直接兼容。
