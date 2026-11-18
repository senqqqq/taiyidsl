# 不同通道混合 Square、ASF RAM 与 POWASF RAM

此前“只允许 POWASF DDS＋TTL”是专用导出器与 mock 的实现范围，
不是不同 DDS 芯片不能使用不同播放模式。现在同一个 shot 可以在不同 DDS 上组合：

- 原生 Square：single-tone profile，提前写参数，定时 IO_UPDATE；
- ASF RAM：驻留幅度包络，使用独立 POW 寄存器；
- POWASF RAM：驻留幅相样点，以相位前缀补偿两条输出管线。

三者使用同一条物理事件时间线。预装和模式准备都在 RF-off 阶段完成，
全部准备结束后再暂存 NCO release，然后按计划分别提交一次 epoch。
运行时保留原有准备、提交和输出效果时间，独立 SPI 可以并行准备。
这次没有把 Square 换成常量 RAM，也没有插 gap、改变包络或合成整段长波形。

实验书写仍按逻辑通道表达。例如已将 `q_square.raman` 和 `q_shaped.raman`
映射到独立 DDS，前者配置 timed Square、后者配置 POWASF：

```python
with exp.average(1), exp.realtime(), exp.parallel():
    play(q_square.raman, Square(200 * ns, frequency=83.123456e6))
    play(
        q_shaped.raman,
        FlatTopGaussian(
            0 * ns,
            sigma=100 * ns,
            cutoff_sigma=4,
            sample_interval=4 * ns,
            frequency=83.123456e6,
        ),
    )
```

两路在同一逻辑边界开始，分别在 200 ns 和 800 ns 结束。实际设备参数、映射和
未实测的示例延迟可参考下方完整示例；相同 Python 顺序不意味着不同通道自动并行。

## Rev8/Rev9 到底是什么

这里的 8 和 9 是 Urukul **CPLD 接口协议修订号**，对应 ARTIQ 驱动中的
proto_rev=0x08/0x09、ProtoRev8/ProtoRev9。它不是 ARTIQ 软件大版本，
也不能直接等同于 PCB 丝印的第 8/9 版。
驱动会依照该协议使用不同的寄存器布局和 SPI 事务。
[官方 Urukul 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)

实际仪器使用哪个协议，取决于板卡及其中实际运行的 CPLD 固件。通常在部署时已经确定；
实验代码中的字段必须与它匹配，不能为了通过排程随便从 Rev9 改成 Rev8。
更换固件是否可行还取决于板卡兼容性，不能把它当作本轮的软件优化。

当前项目中的示例与编译用 device_db 都是离线配置，不能据此认定用户仪器的版本。
应核对实验室真实 device_db、板卡/固件记录及正常初始化时读到的协议版本。
本轮没有读取或操作仪器。

在本机审计条件（SPI coarse=8 ns、分频2）下：

| 路径 | 实际选择调用游标推进 | profile 控制 |
|---|---:|---|
| ProtoRev8 | 416 ns | 四路共享选择引脚 |
| ProtoRev9 | 896 ns | 各通道选择位独立，但调用传输更长 |

这些是接口事务成本，不是 416/896 ns 门长，也不是模拟幅相延迟。
Device.urukul_proto_rev 可对所有 DDS 声明实际协议；示例中全部 DDS 明确采用
一致的协议，POWASF 的 ram_profile_control 也必须匹配。
真实 ARTIQ 9.9664 编译发现，同一个 kernel 中混合 ProtoRev8/ProtoRev9 对象会触发
CPLD.version 的类型统一错误。这是当前真实驱动/编译器的另一项限制，
不同播放模式的混用通过不代表异构协议板卡同 kernel 已通过。

## “200 ns 单门通过”不能理解为任意连续换轴通过

上轮“单门”指每个 shot 中一个 200 ns 门，或该门与另一通道的门配合。
可以在门开始前完成较长准备，因此门长不必大于准备耗时。
反复执行多个 shot，每个 shot 重新准备，也不等同于 shot 内每 200 ns 连续换轴。

| 200 ns 情况 | 当前结论 |
|---|---|
| 一个门，在其开始前预装/选择 | Rev8/Rev9 均可离线验证 |
| shot 内反复调用同一驻留 profile，且相位前缀始终正确 | 可用 IO_UPDATE 重触发；本轮新增正例 |
| X→Y→X，每 200 ns 需要选择不同 profile | Rev8 416 ns、Rev9 896 ns 选择带宽都不足，拒绝 |
| 每门都叫 Y，但首门从初始 X 相位进入 | 首门 X→Y 与后续 Y→Y 前缀不同，可能仍需要换 profile；不能归入同 profile 正例 |

本轮重复正例明确使用初始相位为 X 的 X/X/X/X，profile 与相位样点相同；
它没有频率、幅度或旋转轴切换。单门换成任意预先确定的相位，
也不能证明任意短门序列换轴能力。

## 哪些限制已经解除，哪些仍保留

| 限制 | 来源及当前处理 |
|---|---|
| 独立 DDS 的 POWASF＋Square/ASF 同 shot 被整体拒绝 | 软件实现限制；本轮已解除 |
| 不同 SPI 不能重叠准备 | 本来是 compiler 限制；现在保留绝对时间提交，独立总线允许重叠 |
| 同一 SPI 的事务重叠 | 真实接口资源冲突；必须串行排程，放不下就拒绝 |
| Rev8 共享 profile 引脚影响另一活动 DDS | 真实控制耦合；本轮保守要求相关 bus 上独占活动 DDS |
| 活动 RAM 与其他 DDS 共用 IO_UPDATE | 真实共享更新会重触发 RAM/提交缓冲状态；尚未实现完整联合提交策略，仍拒绝 |
| 同一个 DDS 在 shot 内切换 Square/ASF/POWASF 模式 | 模式/FTW 来源及启动状态处理尚未实现；本轮保持每 DDS 固定模式 |
| native Square 持续每 800 ns 重写不同 profile 参数 | 单次 profile 写入1248 ns，不能因旁边另一路并行而消失 |
| ASF RAM 严格换相时幅相延迟不等 | 仍保留原限制；混合模式不会解除该管线耦合 |

“不同通道”不自动意味着物理资源独立。当前混合正例使用同一 RTIO core 下的
独立 SPI、独立 IO_UPDATE 组；真实接线仍由目标验证检查。
共享 update/profile 的未来联合策略与独立通道混合导出是不同问题。

## 800 ns / 200 ns 验证

examples/mixed_pulse_paths.py 提供以下离线案例：

- 800 ns POWASF X/Y 连续序列与另一通道的 800 ns Square 同时执行；
- 三种路径各自一个 200 ns 门、各自 800 ns 重复门；
- 800 ns POWASF 的选择 SPI 进行期间，另一 DDS 在 400 ns、600 ns 打开/关闭
  一个 200 ns Square，验证不会因为 Python 调用顺序而移动边沿；
- Rev9 同 profile 的四个 200 ns POWASF 门与一个 800 ns Square 并行；
- 三个 800 ns 门后，三种路径各自执行末尾硬件 Rz；
- 同一 qubit 的 Raman 映射 POWASF、RF 映射 Square，一个独立/末尾 Rz 同时更新两者。

Square 重复案例使用相同参数，物理上可形成连续 RF 窗口；不声称每 800 ns 可重写换轴。
ASF 案例故意保留 L_env=112 ns、L_pow=80 ns 的不相等声明，
只验证同轴重播与 RF-off 末尾 Rz，不用相等延迟假设掩盖其严格换轴限制。
所有输出延迟依然是未实测的示例输入。

    uv run --locked python examples/mixed_pulse_paths.py
    uv run --locked pytest tests/test_mixed_pulse_paths.py --basetemp=.test-tmp-mixed
    .\scripts\artiq-python.ps1 scripts/verify_mixed_polar_driver.py output/mixed_pulse_paths/mixed_three_paths_200
    .\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/mixed_pulse_paths/mixed_three_paths_200 --target rv32g --output output/mixed_pulse_paths/mixed_three_paths_200/artiq_compile

输出模型/mock 验证、真实驱动体的虚拟 RTIO 审计、真实 ARTIQ 离线编译和实机验收
分别记录。前三者通过不能升级为实机 deterministic/synchronized/gapless 结论。
本轮没有连接仪器，没有执行真实 kernel，没有测量模拟输出。

2026-11-09 真实离线验收：9 个混合正例及 1 个展开导出通过真实驱动体虚拟 RTIO
审计和 ARTIQ 9.9664/rv32g 编译（20 个 kernel）。另将新版导出器用于原有
1024×800 ns POWASF X/Y 小库重放回归，单独核对驱动轨迹和 2 个 kernel。
最终源码、ELF、驱动版本与测试汇总在 `output/mixed_pulse_paths/verification_summary.json`。
完整测试 782 项通过，Ruff 检查及 wheel/sdist 构建通过。6 个不支持案例按预期拒绝；
另保留真实编译器对同 kernel 异构 ProtoRev8/ProtoRev9 的拒绝记录，
不把它计入通过的 22 个 kernel。
