# 实验书写：时序、复用与多 qubit

当前 DSL 默认顺序执行。不同 signal 不会自动并行；同一 signal 写在 `parallel` 的不同分支中，也不会自动排队。需要同时发生的操作放入 `parallel`，每条完整流程用 `serial` 包住。这套规则能直接表达精确时序，并让编译器检查是否满足设备资源和准备时间。

## 当前时序规则

以下是保留的默认串行/显式 parallel 规则。新增 `Section` 和
`@qubit_operation` 用于复用 Node 子树；需要独立 qubit 自动并发时，显式写
`section(schedule="asap")`。它按 qubit、输出和 frame 依赖排程，退出时合流；
不改变其他范围的默认行为。详见 [Section 与操作](sections_and_operations.md)。
`average(scope=...)` 和 `sweep(chunks=... / chunk_size=...)` 的 host 顺序见
[分块执行](chunked_execution.md)。

| 写法 | 当前含义 |
| --- | --- |
| 连续两次 `play` | 第二次从第一次结束后开始，即使 signal 不同 |
| `with exp.parallel():` | 每个直接子节点从同一时刻开始；退出时等待最长分支 |
| `with exp.serial():` | 内部节点依次执行；整个块可作为一个并行分支 |
| `parallel(align="end")` | 各分支结束时间相同，较短分支向后偏移 |
| `delay(signal, duration)` | 推进所在串行分支的时间；不会让其他分支等待 |
| `serial/parallel(exact_duration=...)` | 检查实际时长是否恰好相等，不自动填充 |
| `repeat(count)` | shot 内重复操作；可用整数扫描参数，当前逐点静态展开 |
| `average(shots, mode=...)` | host 重复采样；cyclic/sequential 只改变点与 shot 的执行顺序 |
| 嵌套 `sweep` | 扫描笛卡尔积；不是多个 qubit 的逐项 zip |
| `case(parameter, value)` | 编译时选择结构分支，不是测量反馈或 Python `if` |

`parallel` 表达时序要求。两个逻辑 signal 若映射到同一 DDS 或互斥资源，编译器仍可能拒绝它们。timed Square 可以在前门输出期间准备下一组参数，支持不额外插 gap 的连续变参；准备吞吐不足会明确拒绝。旧路径和 RAM 仍有各自的空窗限制，不能靠自动推迟脉冲改变实验。见 [Square 时序契约](square_timing.md)。

同一 signal 上相邻且参数完全相同的 Square，可能在物理层合并成一个连续脉冲。`serial` 保证逻辑先后，不会自动产生 RF-off 的低电平间隔；需要分开的两门应明确写 `delay`，并满足所需的准备时间。

## 精确偏移与函数复用

可运行代码在 [`examples/parallel_experiments.py`](../examples/parallel_experiments.py)。其中 `pulse_pair` 是普通 Python 函数，内部仅用现有 DSL：

```python
def pulse_pair(exp, qubit, pulse, *, gap=2 * us, second_phase=1):
    with exp.serial():
        play(qubit.raman, pulse)
        delay(qubit.raman, gap)
        play(qubit.raman, pulse, phase=second_phase)
```

内部 `serial` 使整个函数调用成为一个并行分支。没有这层包装，函数生成的多个节点会各自成为外层 `parallel` 的子节点，并非预期的先后两门。`play` 的覆盖只影响本次调用，不会修改 `pulse` 模板。

```python
pulse = Square(4 * us)
with exp.parallel():
    pulse_pair(exp, q0, pulse)
    with exp.serial():
        delay(q1.raman, 2 * us)
        play(q1.raman, pulse, duration=6 * us)
acquire(q0.readout, duration=10 * us, handle="population", qubits=[q0, q1])
```

这段代码应放在 `average` 内的 `realtime` 中。完整例子得到：

| 操作 | 起点 | 终点 |
| --- | --- | --- |
| q0 第一脉冲 | 0 µs | 4 µs |
| q0 显式 gap | 4 µs | 6 µs |
| q0 第二脉冲，phase=1 rad | 6 µs | 10 µs |
| q1 脉冲，含起始偏移 | 2 µs | 8 µs |
| 共享相机触发 | 10 µs | 20 µs |

join 自然发生在 10 µs。这里 2 µs gap 只在示例设备契约下通过检查，不是任意实验室设备的保证。数字时序对齐也不代表原子处光场已做延迟标定。

## 同时扫描多个 qubit

同一文件提供 `build_parallel_rabi(qubits, durations, ...)`。这是可复制修改的实验脚手架，不是新增 DSL 语法。一个外层 duration sweep 同时用于各 qubit：

```python
duration = SweepParameter("duration", durations, unit="s")
pulse = Square(4 * us)
with exp.average(shots, mode=mode):
    with exp.sweep(duration):
        with exp.realtime():
            with exp.parallel():
                for q in exp.qubits.values():
                    with exp.serial():
                        reset_frame(q.frame)
                        play(q.raman, pulse, duration=duration)
            acquire(
                camera_signal,
                duration=10 * us,
                handle="population",
                qubits=tuple(exp.qubits),
            )
```

未显式覆盖的 amplitude/frequency 来自各 qubit 自己的 Raman 标定，因而无需复制整套脉冲模板。零时长点仍保留逻辑扫描点与统一读出，不产生 DDS 输出。

构建函数只返回 `Experiment`；设备和映射在调用方通过 `lab.compile(exp)` 提供。例子中的 `reference_lab` 是每 qubit 一块板的离线演示；真实使用应加载实验室 `LabConfig`，并按实际相机 TTL 设置 `readout_signal`。

这里所有 qubit 扫描同一个时长轴。各 qubit 使用不同数值表的逐项同步扫描，目前没有 zip-sweep 接口；多个嵌套 sweep 会形成笛卡尔积。现有 `analyze_rabi` 对程序形状还有单路校验，本例不宣称已经提供多 qubit 自动拟合和标定回写。

共享 `acquire` 代表一次触发得到多个 site；不要把它写成每个 qubit 各触发一次。`duration` 仅为 TTL 脉宽，制备、照明、曝光与传输等待仍应由真实实验流程显式安排。

图像入口对每个扫描点、每个选中的 handle 只接受一个逻辑采集。独立拍摄使用不同 handle；如果各 handle 观察的 site 集合不同，可分别创建匹配 ROI 集合的 `ImageResults`。同一帧的多个 site 应关联到同一条 `acquire`。

## 已有复用入口

| 用途 | 已实现入口 |
| --- | --- |
| 单 qubit tune-up | `rabi`、`ramsey`、`randomized_benchmarking` |
| 可追溯 RB 门序列 | `rb_sequences`，包含 seed、Clifford 和 recovery |
| 共享光学参数 | `OpticsConfig`，操作参数与执行顺序分离 |
| 泵浦、Raman、边带冷却 | `optical_pump`、`raman_pulse`、`sideband_cool` |
| 读出流程 | `trigger_readout`、`illuminated_readout`、共享 `acquire` |
| 并行书写示例 | `pulse_pair`、`build_precise_offsets`、`build_parallel_rabi` |

优先使用普通 Python 函数组合已有块。扫描脉冲时长直接传 `SweepParameter`，扫描相同操作的次数直接用 `repeat(parameter)`；不必为每个数值写一份 `case`。只有操作结构发生变化时才需要选择分支，例如不同 RB 门词、push-out 与等长 idle。RB 的任意门词无法仅靠改变 repeat 次数表达，保留 case 比新增一套符号门表 lookup 更简单。

## 关于 Tetris / ASAP

“不同 signal 尽早并行，同 signal 自动顺序排队”可以作为未来显式选择的模式，但当前没有此 API，也不改变默认规则。它需要同时定义同步点、复合操作和 frame 的依赖：同一 qubit 的 Raman/RF 共用 frame，相机触发可能要等待多路驱动，多个逻辑 signal 可能争用同一硬件资源。只维护每根线的时间游标不足以表达这些关系。

当前用 `parallel + serial + delay` 精确控制时序；再把常用流程封装成操作函数，既能减少重复，也能看见脉冲之间的因果关系。

## 运行与验证边界

```powershell
uv run --locked python examples/parallel_experiments.py
```

输出位于 `output/parallel_experiments/`，每个实验保存 `compiled.json`、`timeline.html` 和 `artiq/` 导出程序。导出包含独立 mock 检查，不执行 kernel 或操作仪器；脚本也不自动运行真实 ARTIQ 编译器。真实编译、RTIO 容量、光学同步和相机集成需分别验证。
