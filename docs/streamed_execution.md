# 磁盘编译、执行记录和分页波形

`compile()` 继续返回有完整 `points` 列表的 `CompiledExperiment`。
大扫描用独立的 `compile_stream()`：编译阶段逐点写盘，再一次装载一个块，生成该块的
ARTIQ 源码、Spectrum bank、manifest 和完整离线校验产物。所有块完成后才原子写入
`store.json`；运行器只接受完整、哈希一致的目录。这里不在硬件执行过程中编译下一块。

```python
from taiyidsl import CompilationStore

with exp.average(100, scope="chunk"):
    with exp.sweep(length, chunks="auto"):
        with exp.realtime():
            ...

store = lab.compile_stream(
    exp,
    directory="output/large_scan/compiled",
    host_memory_budget_bytes=256 * 1024**2,
)
store = CompilationStore.open("output/large_scan/compiled")
store.export_report("output/large_scan/viewer", waveform_mode="paged")
```

`directory` 必须不存在或为空。中断的编译目录没有完成索引，不能执行，也不会覆盖重用。
本版不恢复编译进度。原有 `max_points`、每点事件限制继续适用，可显式传入编译选项。

## 自动边界与预算

`chunks="auto"` 仍只标记一个 host sweep 轴，支持任一嵌套层。固定外层坐标后，按轴的
原索引顺序合并相邻轴值；每个轴值的全部内层组合不可拆。加入下一个轴值会超过任一卡
的真实等分 slot、最长 segment、segment 数或 step 数时，关闭当前块。字节完全相同
的量化 segment 按 hash 共享；不是按脉冲数或各点字节简单相加来判断板存。

`compile_stream()` 还以托管工作集预算限制每块大小。预算明确计量为该块各点的规范
JSON 编码字节（logical/physical IR、native 点绑定、diagnostics）及预留的样本工作区。
样本工作区按每 sample/channel 128 字节保守覆盖当前 NumPy 载波、调制、包络、量化及
比较缓冲；编译前相应缩小 tile，渲染入口在分配数组之前再次检查上界。短预算若无法
容纳最小合法 tile，直接拒绝；长 shot 不能凭一个大数组突破该界限。
索引记录 `point_metadata_bytes`、`sample_workspace_bytes`、`managed_bytes`、
`managed_peak_bytes` 和 `boundary_reason`。默认 256 MiB 是这种
确定、可复核的工作集预算，**不是 Python 进程 RSS 上限**。Python 对象开销、调用方
已有 DSL 树、固定元数据、解码器及外部 ARTIQ 编译缓存不在该数字内。
程序不保留全部扫描点的 IR 或波形 recipes；完整索引、轴值和输入 DSL 仍驻内存。

手动 `chunks=N` / `chunk_size=N` 的边界不被自动改动，超过预算直接报错。一个轴值及
其内层点集合仍超限时，应选择更内层轴。单个 shot 自身超限不能靠 sweep 分块解决。
普通 `compile(...chunks="auto")` 也能按板存和 step 自动分块，但它仍保存全部点；
只有 `compile_stream()` 采用磁盘存储和逐块输出。

`store` 没有假装完整的 `.points` 属性。`load_chunk(i)` 返回普通 `CompiledExperiment`，
其点号是块内索引；`store.manifest["chunks"][i]["points"]` 映射回全局点号。
`store.execution_order()` 返回全局 `(point, shot)`。global/chunk 平均的顺序与
[分块说明](chunked_execution.md)一致，默认 global 不变。

## 运行与持久采集

```python
from taiyidsl import RunStore, StreamingRunner

journal = RunStore("output/large_scan/run_001")
runner = StreamingRunner(spectrum_runner, managers=artiq_managers)
try:
    runner.run(store, result_adapter=detector, run_store=journal)
finally:
    journal.close()
```

`spectrum_runner` 是现有已配置的 `SpectrumRunner`；该示例仅说明接口，不会建立硬件
连接。首个确需执行的 shot 先逐块执行 host 目标／别名预检并释放块实例，全部通过后才初始化所有块
用到的控制设备并集一次。`runner.root` 是本会话稳定的 host 根对象，拥有 managers、
ARTIQ 环境、native context 和当前块程序；块的 GeneratedExperiment 是可替换的 kernel
子程序，不是新的会话。根环境不会把旧块加入 ARTIQ 的 children 保留列表。采用
`NativeProgram` 时，组件
对象及参数保存在共享 `native_context`；显式 host_prepare、native initialize 和最终
session finalize 各运行一次。换块只绑定共享对象，每 shot 的 `after_shot` 照常调用。
legacy `NativeKernelPackage` 仍可用普通 `compile()`；stream 明确要求 `NativeProgram`。
进程重启是新硬件会话，不恢复 Python 组件的内存状态，也不宣称相干态连续。

最终样本以 `segments/<SHA-256>.bin` 存在 store 内。同样字节跨块、跨卡只保存一份，
每个块的 bank 仍有独立入口。运行器上传使用只读 `ArtifactSegments` 装载这些已校验
文件，不会把 store 当作可淘汰的临时缓存；旧无该索引的 store 保持原缓存路径兼容。
运行时的程序／设备验证继续保留。

采集适配器的 `before_shot(point, shot)` 可省略。`after_shot` 必须返回 JSON 可序列化
的观测字典，或返回 `ObservationCommit`。需要关联原始图像等文件时：

```python
def after_shot(self, point, shot, *, context, run_store):
    # context 含 run_id、session_id、execution_index、point、shot、attempt。
    path, population = self.camera.collect_and_save(context)
    return run_store.commit_observation(
        context, {"population": population}, assets=[path]
    )
```

适配器若声明 `context` / `run_store` 参数，运行器就传入；旧双参数形式可继续使用。
原始文件需先持久保存，记录保存其路径和 SHA-256。相机触发仍由生成的点 kernel 完成，
host 采集关联用全局 point/shot/attempt，不用块内索引猜测相机帧号。

SQLite WAL/FULL journal 逐次记下 prepared、kernel_entered、hardware_complete、
data_committed、失败及恢复决定。硬件完成和数据提交是两个状态；观测先 fsync 并原子
落盘，再记提交事务。仅内存中追加数组、或 `after_shot` 返回 None，不能宣称数据已保存。
同一 RunStore 只允许一个执行进程，并绑定编译目录 hash。

这三层分工明确：adapter 取得并保存原始数据，`RunStore.commit_observation()` 原子
发布带 context 和资产哈希的 observation 并返回 receipt，runner 再通过
`RunStore.commit()` 校验 receipt 并写入 journal 的 `data_committed`。保存 observation
与 journal 提交之间的中断可以恢复；它们不构成包含硬件输出的数据库事务。

实现位于 `runtime/streaming.py` 和 `runtime/run_store.py`。清理失败不会覆盖导致 shot
失败的原始异常，也不会阻止尝试记录该 attempt 的真实状态和剩余会话清理；附加错误
写入异常 notes。所有清理完成后仍抛出主异常。无主异常而收尾失败时，抛第一个清理
错误，并在 journal 可写时把它记录为会话失败。存储本身不可写时，不承诺补出一条
不存在的提交记录。

成功结束保留 DDS hold，只释放 AWG bank；失败仍 abort。调用者传入的相机、
Spectrum adapters、managers 和 RunStore 由调用者最终关闭。`finish()` 仅在正常
执行完循环时调用；native finalizer、缓存还原、模块卸载和会话记录则逐项尝试。

`runner.resume(store, ..., run_store=journal)` 跳过已提交或明确跳过的点；已有数据必须
仍存在且 hash 一致。硬件完成后、journal 提交前崩溃，若观测已完整落盘则可恢复提交。
无法确认物理 shot 是否执行、或硬件完成而数据尚未保存时，默认抛出 `RecoveryRequired`。
适配器可提供 `recover_shot(point, shot, *, context, run_store)`，只检索该旧 attempt
已经产生的相机数据，返回观测字典／`ObservationCommit`，找不到则返回 None。运行器只在
`hardware_complete` 且尚无已保存观测时调用它，使用原 context；不重新触发或执行
kernel。`unknown` 不走此接口，仍要求显式决定。例如：

```python
def recover_shot(self, point, shot, *, context, run_store):
    existing = self.camera.find_saved_attempt(
        context.run_id, context.execution_index, context.attempt
    )
    if existing is None:
        return None
    return run_store.commit_observation(context, existing.values, assets=existing.files)
```

检查采集端后显式处理，例如：

```python
journal.resolve(execution_index, "retry", reason="采集端确认旧帧作废")
# 或 "skip"：明确记录本次观测缺失，不伪造成功数据。
```

重试创建新 attempt，旧记录不覆盖。`unresolved="retry"` / `"skip"` 可作为显式批量
恢复策略，但默认不会自动重打未知 shot。硬件输出不具备数据库意义的 exactly-once。

## 离线分页和实测入口

打开 viewer 目录中的 `index.html`。主页面只含块索引；选择块才载入该块元数据，选择
时间窗口才载入相交的最终 packed DAC segment。使用本地 classic script 分片，不依赖
fetch、服务器或 CDN，保留完整目录即可在 `file://` 下离线查看。解码后显示逐样本值，
每通道窗口上限 100,000 点，并沿用有界解码缓存；没有为显示重算另一套 RF 波形。

每块链接到实际 bank/entry manifest、ARTIQ 源码和 pulse sheet。分片样本在导出时与
最终 segment hash 核对；模拟电压只是 DAC code 与配置量程的换算，不包括模拟链路。
独立实测对照 HTML 可经 `store.export_report(..., captures=[comparison_html])` 复制并
链接。编译计划、持久执行记录和实测波形是不同证据，离线校验不能替代仪器验收。
