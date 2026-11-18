# ARTIQ 指令时间、游标与输出延迟

数字针对本项目安装的 ARTIQ 9.9664、1 mu=1 ns、coarse C=8 ns、SPI divider=2
（62.5 MHz）、直接 TTL IO_UPDATE、update delay=0。来自真实驱动体的虚拟 RTIO
审计，不是 Python/CPU 执行耗时，也不是实测模拟输出延迟。
复现：`scripts/artiq-python.ps1 scripts/audit_phase_paths.py`。

## 四类时间

| 概念 | 检查什么 | 不能混同什么 |
|---|---|---|
| `now_mu` 计划游标 | 下一事件计划在哪个时间提交 | 不是硬件当前时刻 |
| CPU 执行与排队 | 指令、计算、RPC 消耗了多少真实提前量 | 不一定推进游标 |
| SPI 接口占用 | 总线何时空闲、数据何时完整 | 不是输出已经改变 |
| 输出管线延迟 | IO_UPDATE 到载波/包络效果 | 不等于必须增加门间空白 |

`delay_mu` 和多数驱动中的时间推进是安排未来事件。CPU 可以提前执行这些代码，
RTIO 按时间戳执行。`at_mu` 回退只在有足够提前量且无总线、通道或 SED 冲突时合法。

## 时间线与边沿

| 指令 | 游标变化 | 过程 |
|---|---|---|
| `now_mu()` | 无 | 读取计划游标 |
| `at_mu(T)` | 设为 T | 不等待硬件到 T；可前进或回退 |
| `delay_mu(D)` | +D | 延后后续计划事件，不是 CPU sleep |
| `ttl.on/off`、直接 TTL `dds.sw.on/off` | 0 | 当前游标安排边沿；物理开关响应另算 |
| `ttl.pulse_mu(D)` | +D | on、delay、off |
| `io_update.pulse_mu(8)` | +8 ns | 上升沿提交，8 ns 后下降沿 |
| `with parallel` | 分支同起点，退出取最大结束游标 | CPU 顺序提交，不是多线程 |
| `core.break_realtime()` | max(游标，硬件时间+125000 mu) | 非固定增加 125 μs；只能用于实验时序外建立余量 |
| `core.reset()` | 硬件时间+125000 mu | 清 FIFO 并重置 RTIO，不能作为门间 delay |
| `core.wait_until_mu(T)` | 不主动改变 | CPU 等待硬件到 T |
| `ttl.gate_rising_mu(D)` | +D | 安排输入接收窗口 |
| `ttl.count(T)` | 不改变 | 读输入，可能阻塞至截止时间 |
| 参数换算、普通计算、`set_phase_mode` | 不主动改变 | 消耗 CPU；后者只设置后续驱动行为，不发硬件相位命令 |

相同输出通道在一个 coarse 周期内的多个边沿不能仅靠细时间分辨率保证可接受；
编译器还检查通道碰撞和 SED lane。独立输出的相同时间事件也消耗 lane。

## SPI 与 DDS

| 指令 | 过程 | 游标推进 | 生效方式 |
|---|---|---:|---|
| `set_pow/write16` | 8 位指令+16 位数据 | 416 ns=52C | 缓冲数据，需要提交 |
| `set_ftw/set_asf/set_cfr1/set_cfr2/write32` | 指令+32 位数据 | 704 ns=88C | 不包含 IO_UPDATE |
| `write64` / Square profile | 指令+64 位数据 | 1248 ns=156C | 不包含 IO_UPDATE |
| `set_profile_ram` | 写 RAM 地址、步长、模式描述 | 1248 ns | 不加载采样，也不播放 |
| `write_ram(N)` | 装载 N 个 32 位采样字 | (67N+22)C | 预加载，非播放时长 |
| CPLD `set_profile` Rev8 | CPLD SPI 改四路共用 profile 选择 | 416 ns | 选择变化触发 DDS 更新；输出延迟另算 |
| CPLD `set_profile` Rev9 | CPLD SPI 改所选通道字段 | 896 ns | 四次逐路调用为 3584 ns |
| Rev9 打包四路 `cfg_write` | 一次写四个 profile 字段 | 896 ns | 仍不是 200 ns 实时选择路径 |
| `set_mu(CONTINUOUS)` 普通 single-tone | profile+IO_UPDATE | 1256 ns | update 在 1248 ns 上升 |
| `set_mu(ABSOLUTE/TRACKING)` 普通 single-tone | CFR1+profile+update+再次暂存 CFR1 | 2664 ns | update 在 1952 ns 上升，1960 ns 下降；返回更晚 |

`set_mu` 数字要求入口按 8 mu 对齐，它内部会取整游标。`set(...)` 还做参数换算。
不同 mode、RAM destination、update delay、SPI 时钟、非直接 TTL 更新线必须重新审计。

底层规则：`set_config_mu` 推进 C；n 位 `write` 推进 `((n+1)*div+1)*C`。
例如 `set_pow` 最后一次 RTIO 提交在开始后 8 ns，但传输到 416 ns 才完成；
`write64` 最后一次提交在 712 ns，游标到 1248 ns。不能把最后提交时间当成 SPI 空闲。
51 字 RAM 装载需 27.512 μs，201 字需 107.912 μs；驻留后的播放则可以是 200/800 ns。

## prepare / commit / effect 的读法

```text
prepare 起点       数据准备完成          commit 上升沿        effect
P                  P+416 ns             C                    C+L_pow
```

要在 T 改相位，安排 C=T-L_pow，准备在 C 前完成。IO_UPDATE 的 8 ns 高电平宽度
不再额外加到以其上升沿定义的 L_pow 上。独立总线可以各自完成准备，在各自补偿后的
时刻提交，使输出效果对齐。

历史 592 ns=480 ns 预算+112 ns 示例 RAM 启动延迟，是当时 RF-off 路径的排程要求，
不是芯片固定相位延迟。480 ns 预算与 416 ns 真实驱动占用也要区分。

RAM 包络 L_env、连续 POW L_pow 和 NCO 初始参考延迟分别声明。相同 IO_UPDATE
在 ASF RAM 配合独立 POW 寄存器路径同时重播和换相时，L_pow≠L_env 无法使两个严格效果都落在 T。不能靠第二次提交、
插 gap、修改零尾或将测试延迟改成相等来声称解决。

可选 [POWASF 路径](powasf_timing.md)将相位和幅度采样打包驻留，以相位样点的时间位置
补偿两条输出管线。它另外声明 RAM phase 延迟及 profile 选择调用结束到输出的延迟；
不能沿用独立 POW 寄存器的延迟。样点补偿不改变 `set_profile` 的 SPI 占用：
当前 Rev8 为 416 ns，Rev9 为 896 ns，因此 800 ns 持续换 profile 的吞吐仍须单独检查。

## 来源与验证层级

- [ARTIQ RTIO 语义](https://m-labs.hk/artiq/manual/rtio.html)
- [AD9910 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html)
- [SPI 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/spi2.html)
- [Core 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/core.html)
- [TTL 驱动](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ttl.html)
- [AD9910 数据手册](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9910.pdf)

在线文档版本可能更新，具体数值以审计报告中的本地版本和驱动 SHA-256 为准。
离线轨迹能检查数字提交及驱动时间推进；真实 ARTIQ 编译检查生成代码是否可编译。
时钟同步、模拟幅相响应、CPU/FIFO 持续吞吐和原子处效果仍须单独实机验收。
