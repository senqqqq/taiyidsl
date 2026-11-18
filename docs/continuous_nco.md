# 连续 NCO、严格相位边界与并行 SPI

本页描述 backend 9 引入的 `continuous` 路径。backend 10 的可选
[POWASF 路径](powasf_timing.md)另行支持保持模式的 profile 选择和相位样点补偿；
下文关于 ASF RAM 的限制不代表 POWASF 路径。所有示例延迟均为离线声明，未经过
真实板卡、模拟输出或原子处光学链路验收。旧 `legacy_tracking` 配置保留历史
行为，不能被当作连续 NCO 的实现或证明。

## 配置与支持范围

```python
Device(
    "dds0",
    "dds",
    bus="spi0",
    update_group="board0",
    nco_mode="continuous",
    nco_epoch_latency_mu=80,  # 示例：释放清零到相位参考点；需独立标定
    square_latency_mu=80,  # 示例：single-tone profile 提交到输出
    ram_latency_mu=112,  # 示例：RAM 包络输出延迟
    ram_pow_latency_mu=80,  # 示例：关闭 autoclear 后的 POW-only 延迟
    ram_rearm_duration_mu=4000,
    ram_phase_update_duration_mu=480,
)
```

新字段不从旧的 `ram_phase_latency_mu` 推导。旧字段描述 POW 与 accumulator
autoclear 的联合响应；缺失新契约时明确报错。`nco_mode` 默认 `legacy_tracking`
用于加载旧配置；`reference_lab` 和短门示例显式选用 `continuous`。

连续域限定为每 DDS、每 shot 固定量化 FTW。跨 shot 扫频可以重新建立参考；
shot 内变频不自动 reset 或插 gap。连续模式当前也拒绝 RAM profile 切换：
旧 rearm 会关闭 RAM 并切换 FTW 来源，需要另行实现保持模式的选择路径。

同一 DDS 的 Square/RAM 混用、活动 RAM 共享 IO_UPDATE 的限制仍保留。
独立 SPI 并行的范围是同一 RTIO core，不包括独立 core 的同步启动。

## 一次参考建立与真实 Rz

每个 shot 的 RF-off 预备阶段保持 `phase_clear=1`，装载最终模式及数据。
所有共享提交组成员准备完毕后暂存 `phase_clear=0, phase_autoclear=0`，再以
明确的 `dds_nco_epoch` 事件提交释放。RAM 必须先进入最终 ASF RAM 模式再释放，
避免 NCO 从错误的 FTW 来源开始运行。参考点建立后不逐门清零。

令相位参考点相对 shot 原点为 E0，量化 FTW 为 F，每 mu 的 DDS 时钟数为 C：

```text
POW_epoch = floor(E0 × F × C / 2**16)
POW_gate  = (量化后的目标相位 + POW_epoch) mod 2**16
```

这个修正不随门位置变化。完整的 32 位 accumulator 连续运行；保留小于一个
POW LSB 的固定参考量化误差。相同相位的 RAM 重播不再要求恰好经过整数个载波周期。

`rz(q, theta)` / `Rz` 遵循 `Rz(theta)=exp(-i theta Z/2)`：统一驱动 frame F
减少 theta，驱动相位为 `base + F`，与 shift_phase(frame, -theta) 使用同一更新。
reset_frame 清整个 F，包括 Rz 贡献。后端为 Rz 生成真实准备、IO_UPDATE 提交和相位效果事件，
包括无后续 play 的末尾 Rz、完全没有 play 的独立 Rz。不会多发一个实体 Z 驱动脉冲。

`reset_phase(frame)` 还在最终排程时刻重新建立零载波参考。continuous 路径仍保持
一次 NCO epoch，以量化 FTW 与新参考时刻计算 POW 补偿，不逐门清累加器。
补偿在完整相位精度下合成后再量化；有时间校准时区分逻辑参考和物理生效参考。
显式 pulse phase 和相位校准保留。复位不能打断活动 pulse，准备时间不足仍报错。

```python
with exp.average(1), exp.realtime():
    play(q.raman, shaped_800ns)
    rz(q, math.pi / 2)  # 800 ns 处真实更新载波参考；没有后续 play 也保留
```

硬件目标为已映射的 `q.raman/q.rf`，包含未播放的已映射 drive；不影响独立照明或
读出通道。若不需要某个 drive，不应把它作为该 qubit 的相位目标映射进来。
多个 drive 共用不兼容的 DDS 状态/更新组时，编译器拒绝而不是漏更新一路。
Rz 目标 DDS 需要独立的相位所有权；同 shot 内若还有非 drive 信号使用该 DDS，
当前会保守拒绝，避免相位更新误作用于照明等其他逻辑通道。

生成代码的准备和提交是分开的，以下为 RAM POW 路径的结构：

```python
at_mu(t0 + prepare_time)
self.device_0.set_pow(new_pow)  # 真正写硬件缓冲寄存器
at_mu(t0 + phase_time - L_pow)
self.device_0.io_update.on()  # 相位效果在 phase_time
at_mu(t0 + phase_time - L_pow + 8)
self.device_0.io_update.off()
```

Square 对应写 profile 的 `write64`；不能把单独 `set_pow` 当作普通 single-tone
profile 的相位写入。实际生成示例见 `output/continuous_nco/standalone_rz/experiment.py`
和 `output/continuous_nco/ram_800_terminal_rz/experiment.py`。

下面摘自独立 `Rz(+π/2)` 的生成代码，仅添加时间注释。该例没有任何 `play`，
DSL 中 Rz 位于 0 ns；RF-off 预备阶段使逻辑原点对应 `t0+1488`，不改变门间边界。
原 POW=23592，新 POW=7208，差值为 −16384，正好是 −π/2，且 FTW 不变。

```python
# 装载完成后，仅在 shot 预备阶段提交一次 clear 的释放。
at_mu(t0 + 0)
self.device_0.io_update.on()
at_mu(t0 + 8)
self.device_0.io_update.off()

at_mu(t0 + 128)  # prepare: 1248 ns 真驱动占用，1280 ns 声明预算
self.device_0.write64(0x15, (8192 << 16) | 7208, 343597384)
if now_mu() > t0 + 128 + 1280:
    raise ValueError("Square prepare driver exceeded declared budget")
at_mu(t0 + 1408)  # commit: 提前 L_square=80 ns
self.device_0.io_update.on()
at_mu(t0 + 1416)
self.device_0.io_update.off()
# effect: t0+1488，即 DSL 的 Rz 时间 0 ns；不依赖后续 play。
```

RAM 末尾 Rz 示例采用三个 800 ns 门。其逻辑原点为 `t0+240`；表中 opcode 7
在 `t0+2080` 调用 `set_pow(29507)`，从原值 45891 减去 16384；在 `t0+2560`
提交，经过声明的 80 ns 后于 `t0+2640` 生效，即逻辑 2400 ns。包络在相同边界
结束，RF 关闭，没有下一门；因此这里不要求 L_pow 与 L_env 相等。

## 严格边界与能力矩阵

相位命令的 carrier effect 必须严格位于 DSL 时间；不允许在前门原有零尾提前改变。
普通 play 在完全 RF-off 时可以预设参数；相邻门切换不得改变前门任何时段。

ASF RAM 的一次 IO_UPDATE 同时提交 POW 并重启包络。若下一门和相位更新都要求 T：

```text
C_env = T - L_env
C_pow = T - L_pow
```

一次提交要同时满足两者，需要 L_env=L_pow。两次提交会再次重启 RAM；连续 NCO
和并行 SPI 都不解除此耦合。独立 RF-off Rz 可以采用不同延迟，但其内部 RAM
重启仍不得干扰后续门。示例 80/112 不等时，800 ns 的原有零尾也不能成为例外。

| 场景 | 离线契约结果 |
|---|---|
| 800 ns 同轴 Square/RAM 连续重放，任意可表示固定 FTW | 支持，无逐门 clear |
| 200 ns 单个 Square/RAM，配合其他通道 | 支持提前准备，需满足具体资源窗口 |
| 800 ns native Square 持续逐门换轴 | 当前每次 1248 ns profile 写入仍不足 |
| 800 ns RAM 换轴、幅相延迟不同 | 严格边界拒绝 |
| 800 ns RAM 换轴、模拟相等延迟、独立 SPI | 排程支持；相等延迟仅模拟测试，非实机结论 |
| 两路共用 SPI，每 800 ns 各写一次 POW | 当前每次真实 416 ns，加总已超过周期 |
| 200 ns 持续逐门任意换轴 | 当前控制吞吐不足，留待后续高速控制路径 |
| 末尾/独立 Rz | 有真实硬件提交；准备窗口不足时拒绝 |

## SPI 并行与验证

缓冲写入按 bus 排程，独立 SPI 可以重叠并跨越无关 TTL/RF 边沿。未知 bus 保守
串行；同 bus 的传输、数据准备与提交仍有依赖。导出保留真实驱动调用，使用计划内
绝对 `at_mu` 回退，不把整个 shot 分成两个长 parallel 分支。

验证检查真实 bus/update 别名、SPI busy、尚未提交的缓冲状态、提交前数据完成、
实际 RTIO 提交顺序的 SED lane 和通道碰撞。某些需拆分 SPI 事务才能重排的情况
仍可能被保守拒绝；不会因此插 gap。CPU/FIFO 长时间吞吐需要实机另验。

Square 仍走 single-tone，FlatTopGaussian 仍走驻留 ASF RAM。不会按波形长度自动
降低采样率；`sample_interval` 显式设置，满足 `4*step/SYSCLK`，超过容量报错。
默认 20 ns，短门示例 4 ns。长门序列复用小波形库，不拼接整个序列。

运行 `uv run --locked python examples/continuous_nco.py` 生成成功和拒绝案例。
`examples/short_gate_timing.py` 给出 800/200 ns 完整矩阵；
`examples/ram_boundary_sequence.py` 默认演示 1024 次 800 ns 同轴调用。
硬件样例、真实 ARTIQ 编译、软件/真实驱动离线轨迹与实机验收必须分别报告。

完整指令时间表见 [ARTIQ 指令时间参考](artiq_instruction_timing.md)。

## 2026-11-06 离线验收记录

- 完整回归 612 passed；Ruff lint/format 通过。
- 9 个接受案例通过导出/mock 等价、真实 ARTIQ 9.9664 编译（18 个 kernel）以及
  真实驱动体的虚拟 RTIO 审计；4 个不支持案例按预期拒绝。
- 汇总在 `output/continuous_nco/verification_summary.json`，包含源文件、ELF、驱动
  及审计脚本哈希。每个案例目录分别保存 `artiq_compile/compile_report.json` 和
  `real_driver_trace.json` 或 `real_ram_driver_trace.json`。生成文件头与原始导出
  manifest 保留导出时的未编译状态，后续真实编译结论由匹配源文件哈希的独立报告证明。
- 指令审计在 `output/continuous_nco/driver_instruction_times.json`；复现命令为
  `scripts/artiq-python.ps1 scripts/audit_phase_paths.py --output output/continuous_nco/driver_instruction_times.json`。
- 没有执行硬件或 kernel。模拟幅相延迟、板间同步与持续提交吞吐仍未实机验收。

因此，deterministic 指已检查的逻辑/准备/提交/效果契约；synchronized 指按声明
延迟对齐的多路输出计划；gapless 指接受案例保留原门边界及包络、无额外间隙。
这三个软件层面的结论都有上述适用范围，不等于任意门序列或真实光学输出已通过验收。
