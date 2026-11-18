# 最终 DAC 样本与捕获波形对照

本工具离线读取 NPZ，不控制示波器。它把捕获与指定编译产物的最终 packed DAC
数据关联起来，而不是根据逻辑 pulse 重新生成一条正弦。曲线中的电压是按输出量程
换算的标称 DAC 电压，未模拟重建滤波器、放大器、AOM 或光场。

```python
from taiyidsl import write_capture_comparison

paths = write_capture_comparison(
    compiled_or_store,
    "capture.npz",
    capture_metadata,
    "output/comparison.html",
    exploratory_alignment=False,
)
```

NPZ 的必需数组为一维 `voltage`（伏特）。可选 `time_s` 必须与声明的采样率及起点
一致。默认最多100,000个捕获样本，并限制解码窗口；长捕获先明确截取要对照的短窗口。
如果还提供 envelope/reference_phase_rad 和 phase_uncertainty_deg，可复用已有
相位误差拟合诊断；该诊断不会消除原始波形残差。

metadata 必需字段：artifact_sha256、全局 point、shot、attempt、run_id、group、
card、channel、sample_rate_hz、time_origin_s、deskew_ns、calibration_revision、
synthetic。synthetic 必须明确为布尔值；合成例为 true。普通编译产物的hash对应
其完整性hash，磁盘产物使用 CompilationStore.sha256。捕获的校准版本必须与该组
编译时的校准版本一致。

`time_origin_s` 表示捕获第一个样本相对逻辑shot T0的时刻。正的 deskew_ns 表示
已独立测量的探头/线缆延迟，对照时从捕获时间中减去它。不同采样率时按最终DAC
样本作零阶保持参考，不默认拟合时移、幅度或相位来缩小验收误差。

HTML 单独内嵌 Plotly，断网可打开；配套JSON保存来源、原始残差和捕获文件hash。
`exploratory_alignment=True` 可另外搜索小范围时移及增益/DC拟合，曲线明确标为探索；
原始残差不被替换，physical_acceptance 始终为 not_evaluated。是否满足实验室幅相
指标仍需独立的同步采集与验收程序，不从一张对照图直接推出。

演示：

```powershell
uv run --locked python -m examples.artiq_spectrum.capture_comparison
```

示例故意加入10mV偏置，验证即使探索性拟合很接近，原始误差仍被保留。它只产生
合成数据，不是两卡输出或原子端效果的实测。
