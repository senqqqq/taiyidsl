# 读出 adapter：触发、图像与持久结果

相机 TTL 只触发拍照，不会自动创建图像、`acquire` handle 或分类结果。
ARTIQ/native 负责定时边沿；host adapter 负责相机布防、传输和保存；
`ImageResults` 负责已有图像的 ROI 计数和 occupancy。
这些步骤的真实硬件配置与标定属于实验室包。

## 选择已有运行路径

类型从 `taiyidsl.runtime.readout` 导入，无需继承，也没有运行时 `isinstance` 检查：

| 类型与路径 | 必需回调 | 返回值和索引 |
| --- | --- | --- |
| `ResultAdapter`，普通导出程序的 `result_adapter` | `before_shot(point, shot)`、`after_shot(point, shot)`、`finish()` | 回调返回值不参与保存；adapter 自行保存。索引属于该导出程序；单独运行 chunk 导出时不能假设它是全局 point |
| `PersistentResultAdapter`，`StreamingRunner` | `after_shot(point, shot, *, context, run_store)` 是推荐的类型签名 | 返回可 JSON 编码的 `dict` 或 `ObservationCommit`；point/shot 是整个 CompilationStore 的全局索引 |
| `RecoverableResultAdapter`，持久路径的可选能力 | `recover_shot(point, shot, *, context, run_store)` | 返回原 attempt 的 dict／receipt；找不到已保存数据则返回 `None` |

持久路径的 `before_shot` 和 `finish` 可省略，所以它们不列为持久协议的必需方法。
已有仅接受 `(point, shot)` 的持久回调仍然有效；runner 根据参数名字按需传入
`context`、`run_store`。如需要这两个参数，应明确写出名字，不能只写 `**kwargs`
就假定 runner 会提供它们。两个协议分别描述现有路径，不代表新统一 runner。

`finish()` 只在正常完成循环后调用，不是异常情况下必达的资源清理函数。
实验室代码应在外层 context manager／`try/finally` 中管理真实相机连接。
`HardwareSession.run()` 目前没有 detector adapter 注入入口，仍拒绝带 acquisition
的任务；不能把本文回调传给它后假定已有图像读出支持。

## 数据流与失败边界

1. `before_shot` 布防并记录当前 point/shot/attempt；不在这里偷偷增加实验触发。
2. native 或 DSL 的相机 TTL 按已编译计划执行。触发宽度不等于曝光时间或传输等待。
3. 硬件返回后 `after_shot` 取得帧。空位是有效图像分类为 0；缺帧是 unavailable，
   occupancy 保持 NaN／JSON null，不能填充全零假图像。
4. 持久路径先保存原始帧并完成写入，再调用
   `run_store.commit_observation(context, values, assets=[raw_path])`。
   返回 receipt，由 runner 更新 journal；assets 会记录文件哈希。
5. 已完成硬件但数据尚未提交时，恢复只查找同一个 `ShotContext` 的既有数据。
   不重新 arm、trigger 或运行 kernel；找不到时默认停止。硬件结果为 unknown 的
   attempt 不走这个回调。重试或跳过必须显式决定。

`ShotContext` 包含 run、execution index、point、shot、attempt 与 session 身份。
它和 `ObservationCommit/RecoveryRequired` 的唯一类型定义在
`taiyidsl.runtime.contracts`，顶层入口继续导出同一对象。`RunStore` 的实现位于
`taiyidsl.runtime.run_store`；它维护执行 journal，`results` 只处理已有观测及存档。
保存时不要仅用 point/shot 命名覆盖文件；相同实验可能有多次显式 attempt。
直接返回 dict 适合小型数值结果；原图使用单独文件及 receipt，避免把 NumPy 数组
放进 JSON。保存了 receipt 但 journal 尚未更新时，RunStore 可自动核验并恢复提交。

## 实验室包示例

见 [完整可运行示例](../examples/lab_extension/README.md)。它包含：

- 使用 `NativeModule.from_package` 的相机 TTL 组件声明；DSL 环境不导入 ARTIQ。
- 保留普通两参数回调的 `CameraAdapter`。
- 持久 adapter，将原始帧与完整 attempt 身份存为 NPZ，再提交观察值和文件哈希。
- `recover_shot` 只读取已保存帧；损坏或身份不匹配的文件报错。
- 人工构造的 occupied、empty、missing 三个案例；没有连接相机或运行原生 kernel。

示例使用显式 `ResultSet` schema 关联 native 触发图像。若实验用 DSL `acquire()`，
应优先通过 `ImageResults.from_compiled(...)` 校验每个 handle 对应的采集事件。
跨设备的 frame ID／硬件时间戳关联、曝光、超时、丢帧和缓存策略应由真实 adapter
明确实现，不能用取回帧的列表顺序替代这些验证。

更详细的图像模型见 [结果与拍照](results_and_imaging.md)，恢复语义见
[分块执行](streamed_execution.md)。
