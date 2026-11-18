# Host 扫描分块与 Spectrum 驻留执行

分块控制一次上传哪些扫描点，以及这些点与重复 shot 的执行顺序。它不拆开一个
shot 内的相干门序列，不改变该点的 pulse 时刻、相位、采集坐标或准备／读出内容。

```python
length = SweepParameter("length", [1, 2, 3, 4], "dimensionless")
with exp.average(100, mode="cyclic", scope="global"):
    with exp.sweep(length, chunks=2):
        with exp.realtime():
            # 原来的准备、triggered_sequence 和读出
            ...
```

`chunks=2` 表示两块，分别含 length 的前两个和后两个轴值。`chunk_size=2` 表示
每块最多两个轴值；两者互斥。整数 `chunks` 必须为正整数且不超过该轴长度，分不尽时
前面的块多一个轴值；`chunk_size` 也必须为正整数，最后一块可以较短。
没有分块参数时，各点仍分别形成上传单元，原有采样顺序保持不变。
`chunks="auto"` 按该轴的连续值自动选择满足所有活动卡容量限制的边界；
磁盘编译、托管预算和自动分块细节见[流式编译说明](streamed_execution.md)。

## 分在任一 host sweep 层

首版允许现有嵌套 host sweep 链中的一个轴分块。固定所有外层坐标后，取该轴的
连续索引区间，并包含其中每个轴值的所有内层组合。例如：

```python
with exp.sweep(detuning):
    with exp.sweep(length, chunks=2):
        with exp.sweep(seed):
            ...
```

每个 detuning 独立分为两块；每块包含相应 length 及全部 seed。重复的轴值也按
各自索引保留，不去掉重复点。更外层分块通常包含更多点，更内层分块更细；不要求
只能最外层或最内层，但不能同时分多个轴，也不能在 realtime 内上传或分块。

## 两种平均范围明确选择

`mode` 继续选择 cyclic／sequential，新增 `scope` 选择平均范围：

| 配置 | 执行顺序 |
| --- | --- |
| `mode="cyclic", scope="global"`（默认） | 每个 shot 依次遍历全部扫描点 |
| `mode="cyclic", scope="chunk"` | 当前块内完成全部 shot，再处理下一块 |
| `mode="sequential"` | 每个点完成全部 shot，再处理下一点；这时两种 scope 的点顺序相同 |

以四个点、两块、两个 shot 为例，记 `p0/s0` 为点 0 的 shot 0：

```text
global: p0/s0 p1/s0 p2/s0 p3/s0 p0/s1 p1/s1 p2/s1 p3/s1
chunk:  p0/s0 p1/s0 p0/s1 p1/s1 p2/s0 p3/s0 p2/s1 p3/s1
```

默认 global 保持原来的全扫描交错采样，也意味着每轮可能重新载入各块。
chunk 可以减少上传，但改变了点的时间顺序及对漂移的敏感性，因此必须显式指定。
`scope="chunk"` 要求存在分块轴。

编译结果的 `host_execution_plan` 是紧凑计划，只保存块与点的关系，不展开
point×shot 的大列表。`compiled.execution_order()`、导出 host 循环、离线结果和
pulse sheet 的 execution index 都使用这份计划。结果仍按 `[point, shot, qubit]`
索引；实际执行顺序另行记录，不能从数组位置推测 host 墙钟时间。

## 板上共享波形与独立入口

每个块、每个活动同步组、每张卡建立一份驻留 bank：

- 最终量化样本完全相同的 segment 按样本长度和 SHA-256 共享。
- 每个点有独立 step 子图和 `entry_step`，在自己的 END 结束，不自动跳到下一点。
- 首次使用 bank 时，全卡预检通过后才 configure／upload。每个 shot 仍分别选入口、
  全卡 arm／ready、运行 ARTIQ 点 kernel、等待所有活动卡完成。
- 重复使用同一 bank 不重复 DMA；切换 bank 时重新载入。磁盘 `SegmentCache` 与
  板上驻留是两层不同的缓存。
- 同步组活动时，组内无脉冲的卡仍运行零样本程序。整个组在该点无输出时不 arm／
  触发该组；整个点无 Spectrum 工作时保留原 ARTIQ 执行。
- 上传、选入口、arm、ready、kernel 或完成检查失败时，按已触及卡的反序 stop，
  并作废驻留状态。任何卡未 ready 都不能进入该点 kernel。

`SpectrumRunner.invalidate()` 用于调用方重新配置卡后显式作废驻留假设。不得在
runner 外修改板存后仍要求它把旧驻留标记当作有效数据。时钟变化仍要求重新校准；
驻留复用不会省略每 shot 的设备／时钟与校准检查。

容量按每张卡分别检查，不能只比较所有 segment 的字节总和。限制包括启用通道数、
等分 memory slot、最长 segment、segment 数与整个 bank 的 step 数。显式分块在
compile 阶段使用已有 hash／sample_count 做联合容量预检，不重复生成样本；导出时
验证每个入口的最终播放与独立点程序一致，运行时再检查设备真实容量。

手动分块超限会指出 chunk、card 和限制，不偷偷改变块数。若一条 RB shot 自身已
超限，切 sweep 无法解决；需要另行改变表示方式或采用经过验证的仪器流式方案。
普通 `compile()` 仍保存全部点的 IR／waveform recipes；新的 `compile_stream()` 逐点
落盘并逐块导出，工作集预算和进程 RSS 的区别见上述文档。

## 导出、记录与兼容

新的混合 manifest 使用 `schema_version=2`，保留 `points` 中的逐点程序便于核查，
增加 `host_execution_plan` 和 `banks`。每个 bank 保存 `index`、global `points`、
`groups` 和内容 `sha256`；每张卡的 bank program 保存共享 `segments`、全局 `steps`、
`memory` 及包含 `point/entry_step/step_count/segment_ids` 的 `entries`。
样本相同但来源不同的 recipe 仍可共享 slot；每个 entry 另外保存原 recipe 来源，
同时独立校验实际上传的共享 slot，避免把去重后的来源误记成另一扫描点。

新 runner 继续接受 schema 1，并沿用旧的逐 shot 上传路径。schema 2 使用
`load_bank()`／`run_resident_point()`；通常由兼容入口 `run_point()` 自动分派。
`SpcmAdapter.select_entry()` 只选择停止卡上的有限程序入口，不会自行触发输出。

成功执行记录包含 point、shot、execution index，以及 chunk、bank SHA-256、是否
发生上传、各卡入口和状态。无 Spectrum 工作的点也有完成记录；失败不会产生
“已完成”记录。报告中的计划与 SDK 参数清单仍不是执行记录，mock 通过也不是
板卡实测。双卡触发／幅相／实际 re-arm 行为需按实验室验收单独验证。
