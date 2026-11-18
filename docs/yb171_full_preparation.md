# ¹⁷¹Yb 完整制备与读出的参考边界

本页把完整装载流程、当前可编译的光学操作、相机采集和数据解释分开定义。配套 `examples/yb171_preparation_reference.py` 默认只生成离线文件，不访问硬件。

## 文献中的完整流程

参考 Jenkins 等的 [Ytterbium Nuclear-Spin Qubits in an Optical Tweezer Array, PRX 12, 021027 (2022)](https://doi.org/10.1103/PhysRevX.12.021027)，尤其是 [附录 A、B.3 和 D 的公开全文](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)。这是一条具体的 Yb 实验路线，不代表所有中性原子实验都采用相同顺序。

| 阶段 | 物理作用 | 本项目需要的能力 |
|---|---|---|
| 原子源与蓝 MOT | 399 nm 捕获和预冷却，向科学腔提供原子 | 原子源、MOT 光束、磁场梯度控制 |
| 绿 MOT | 556 nm 宽线捕获、窄线冷却、压缩 | 失谐调制、强度 ramp、磁场梯度 ramp |
| 光镊装载 | 捕获原子、清除未捕获原子、单原子占据制备 | 光镊阵列、偏置场、装载光束 |
| 阱内冷却 | 形成初次成像前的低温样品 | 冷却光、磁场和阱条件 |
| 初次成像 | 确认各位点是否装载 | 成像光、同时冷却、相机曝光 |
| 自旋/运动制备 | 光抽运，按实验需要进行 Raman sideband cooling | 泵浦光、Raman 光束、控制场条件 |
| 实验与读出 | 相干控制后直接成像，或先进行态选择性移除 | 读出态映射、场/阱切换、末次成像 |

文献用前后两次成像识别初始占据和最终存留；核自旋制备与读出使用各自的光学过程。本页没有把文献中的磁场、功率和时长复制成实验室默认值。

## 当前可运行的示例

```powershell
uv run --locked python examples/yb171_preparation_reference.py
```

输出目录为 `output/yb171_preparation_reference/`：

- `workflow.json`：完整阶段说明、各阶段需要的能力、外部 builder 名称和数据含义。
- `control_segment.json`：只包含当前支持操作的编译结果。
- `control_segment.html`：对应的离线时序图。

默认编译片段为：

```text
外部已经完成装载、预冷却和工作条件准备
    → pre 成像（无 push-out）
    → sideband_cool（含初始 optical pump）
    → post 成像（默认无 push-out，观察存活）
```

`build_control_segment(..., rounds=0)` 仍保留一次初始光抽运；这沿用现有 RSC 语义。`pushout=True` 会在末次成像前执行示例 push-out 序列，此时结果含义改为 `pushout_survivors`。

这个片段要求实验室预先提供能支持所选成像和控制操作的场、阱及光学条件。若实际实验需要在初次成像后改变磁场或阱深，必须显式补上相应操作。不能把数值等待视为已经设置或验证了这些条件。

## 用普通函数组合完整序列

示例同时提供 `build_full_sequence()`，要求调用者明确提供三个普通 Python builder：

```python
builders = {
    "load": load_atoms,
    "enter_control": enter_control_conditions,
    "readout": final_readout,
}
exp = build_full_sequence(q0, optics, stage_builders=builders)
```

这些函数由实验室实现，示例没有提供空实现。缺少任意函数时会在开始构建之前报错。

| 函数接口 | 必须完成的工作 |
|---|---|
| `load(exp, qubit, parameters)` | 从装载开始，到初次成像所需条件就绪；包含 MOT、光镊装载、预冷却等 |
| `enter_control(exp, qubit, parameters)` | 从成像条件切换到光抽运/RSC 条件，声明 ramp、稳定时间和资源 |
| `readout(exp, qubit, parameters, *, handle)` | 完整末次读出：需要时作态映射/push-out、场和阱切换、照明，以及使用所传 handle 的采集声明 |

完整组合顺序固定为 `load → pre 成像 → enter_control → RSC → readout`，适合作为制备诊断的起点。开展其他实验时，直接在 Python 实验函数里把相干控制插入 RSC 和 readout 之间。

builder 在构建 IR 时执行一次，必须生成 DSL 操作，不能在里面连接设备、读取实际图像或根据实时占据做 Python 分支。传入函数只表示提供了构建实现，不代表 MOT 或读出已经通过物理验收。元数据持续记录 `physical_validation=not_verified`。

当前 ARTIQ 参考后端支持的输出仍受其能力声明约束。磁场电流和光阱 ramp 需要对应设备适配与时间/资源契约；`workflow.json` 中声明需要这些能力，不会自动创建它们。本轮不加入原子重排、装载重试或测量反馈。初次成像的装载失败保留在结果中，离线选择统计样本。

## prepare、measure 和结果的分工

`prepare(q, protocol=...)` 的旧接口只展开配置里的触发序列。它可以触发实验室已实现的外部装置，但一个名为 `standard` 的 TTL 脉冲并不证明已完成 MOT、冷却和自旋初始化。完整物理制备通过上述普通函数明确组合；LabConfig 保存接线与设备契约，OpticsConfig 保存工作点参数，执行顺序保留在实验函数中。

`measure(q, protocol=..., handle=...)` 的旧接口同样描述触发和采集位置，不在构建时返回实测数值。示例通过 `illuminated_readout()` 明确给出照明、冷却、相机触发和等待。曝光宽度与 TTL 触发宽度不同，固定传输等待也不能替代真实采集完成检查。

实验室接入相机后，主机端收集图像并核对 shot、采集 handle、相机帧号与时间；离线层负责 ROI 积分、背景扣除和占据分类。一次曝光可能覆盖多个 site，应保存一份原始图像并关联位点，而不是逐 qubit 重复触发相机。

## 必须保留的数据语义

- `pre`：初始占据。初始空阱是成功拍摄后的有效观察，不是丢帧。
- `post`：末次占据。是否代表存活或态选择性移除后的幸存者，由读出协议决定。
- `validity`：相机是否成功获得该帧、位点是否可判别；不能由 occupancy 的 0/1 代替。
- 原始图像、ROI、分类阈值/算法版本、site 顺序、曝光信息和参数快照应随结果保留，以便重新分析。

无 push-out 的存活统计采用：

\[
P(\mathrm{post}=1\mid\mathrm{pre}=1,\;\text{两次采集均有效})
\]

其中 `post=0` 要进入分母。push-out 读出中的空位可能是目标态被移除的信号，也可能来自非目标损失；仅凭两幅占据图不能无歧义地逐 shot 区分二者。需要参考条件和读出标定后才推断态布居，不应直接把所有空位标为 `atom_lost` 并过滤。

## 验证范围

配套测试检查前后采集 handle、构建顺序、缺失 builder 的提前拒绝、描述信息的保存，以及默认运行只导出受支持的片段。它们没有检验装载效率、温度、自旋初始化保真度、真实相机传输或原子处波形。实机接入时，应分别记录离线验证、真实 ARTIQ 编译和物理验收状态。
