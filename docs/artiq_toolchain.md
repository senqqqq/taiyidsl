# ARTIQ 工具链、RPC 与 device_db

本项目面向 ARTIQ 9。DSL 本身继续使用 uv 管理的 CPython 3.12；ARTIQ 使用
独立的官方 MSYS2 发行环境及其捆绑 Python，不把两套依赖混装。
安装位置为 `.toolchains/artiq9`，不进入 Git，也不修改全局 PATH。
这里安装的是实验编译和运行所需的软件；不涉及重新生成或烧写 FPGA gateware。

## Windows 安装来源

- [M-Labs 官方安装说明](https://m-labs.hk/artiq/manual/installing.html)
- [固定构建 202416](https://nixbld.m-labs.hk/build/202416)
- 文件：`artiq-msys2-x86_64-9.9664+071b41b.exe`
- 文件大小：457787346 bytes
- SHA-256：`de60e9b448f31fcb925993bfc3c51630cb8fcf562eaae32fa73be6db8c9effce`

完整安装包放在 `.toolchains/downloads/`。安装器的具体调用和安装验证记录保存在
本机 `.toolchains/` 中。官方稳定下载地址会随时间变化；复现时应核对上述固定构建
和哈希，不把下载到文件视为安装或编译已经成功。

2026-10-30 安装完成并验证：ARTIQ `9.9664+071b41b`、捆绑 Python `3.14.5`、
llvmlite `0.47.0`、LLD `22.1.4`。官方安装器建立了开始菜单快捷方式和卸载器。
初次 MSYS2 初始化的可选联网密钥刷新等待过久，结束该刷新子进程后，安装器正常
完成并返回 0；捆绑密钥已初始化，签名校验设置未更改。后续联网更新前可重新运行
官方密钥刷新流程。

启动脚本也把本次进程的 TEMP/TMP 指向工具链内目录，避免 Windows 沙箱临时目录
导致链接器无法写入输出，结束时恢复原环境变量。发行版源代码未作修改。

项目根目录下可通过 PowerShell 启动器临时设置这套环境的 PATH：

```powershell
.\scripts\artiq-python.ps1 -m artiq.frontend.artiq_compile --version
.\scripts\artiq-python.ps1 -m artiq.frontend.artiq_run --help
```

不要通过普通 `uv add artiq` 替代完整的编译工具链。

## 为什么实验文件里没有 device_db？

`device_db.py` 是 ARTIQ 的外部设备数据库，包含驱动模块/类、构造参数、设备名、
RTIO 通道、SPI/CPLD/DDS 关联、core 网络地址和时钟等。ARTIQ 的启动器或 master
加载这份数据库，实验通过 `self.get_device("urukul0_ch0")` 获取已命名的驱动对象。
因此普通实验不需要在自己的源码里导入或内嵌 `device_db`。

当前导出已经生成 `get_device()` 调用和所需设备的 manifest，但没有生成真实
实验室设备数据库。LabConfig 中的逻辑信号、时钟和资源模型还不足以确定实际
RTIO/EEM 分配、core 地址、DDS chip select 和所有驱动构造参数。
不能从逻辑设备名推断出这些真实连接。

各配置的职责是：

| 配置 | 职责 |
| --- | --- |
| LabConfig | DSL 信号映射、后端资源、时钟与时序/路径标定 |
| OpticsConfig / Qubit | 共享光学参数与各 qubit 的实验标定 |
| ARTIQ device_db | 用实际驱动连接具体设备，必须与 gateware 拓扑一致 |

实际运行时应提供实验室已有、与硬件一致的数据库，由 ARTIQ 启动器传入。
修改数据库不会改变 FPGA 中的硬件配置。
详见 [ARTIQ 设备数据库说明](https://m-labs.hk/artiq/manual/environment.html#device-database)。

## RPC 是什么？

RPC 是 Remote Procedure Call（远程过程调用）。在 ARTIQ 实验中，典型情况是
core 上运行的 kernel 请求主机电脑执行某个 Python 方法，再获取返回值。
kernel 调用普通 Python 方法时可以隐式产生 RPC；`@rpc` 可显式标注并设置行为。

例如，kernel 获取计数后，可调用主机方法记录数据、写文件或调用分析库。
这些库不需要、也通常不能在 core 的受限 Python 环境中运行。
同步 RPC 会等待主机返回；异步 RPC 可把处理提交给主机，但仍有通信/排队成本。
它们都不能被当作具有固定延迟的纳秒级门操作；需要根据实时预算安排在合适边界。

当前生成代码的 host `run()` 负责扫描、重复、调用 shot kernel 和结果 adapter。
kernel 返回后由 host 调用 adapter，是普通主机 Python 调用，并不自动等于 RPC。
ARTIQ 设备 controller 之间也使用 RPC，但这是设备服务通信的另一种用途。

参考：[kernel 与 RPC 语义](https://m-labs.hk/artiq/manual/core_language_reference.html)。

## 无仪器进行真实编译

先在 DSL 环境生成实验，再用 ARTIQ 环境编译：

```powershell
uv run --locked python examples/offline_imaging_workflow.py
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/offline_imaging/artiq --target rv32g --output output/artiq_compile/offline_imaging
```

`rv32g` 是显式选择的参考 CPU 目标，不代表已检测到实验室的 core 型号。
工具还接受 `rv32ima` 和 `cortexa9`，实用时必须选择与目标设备相符的类型。

`examples/artiq_offline_compile.py` 自动构造 `device_db_compile_only.py`：
使用真实 ARTIQ 驱动，`core.host=None`，没有网络 controller。
通道和板卡连接仅为编译占位配置，文件明确标注不能用于硬件。
程序调用 `Core.compile()`，覆盖初始化和全部导出的 kernel/参数行；不调用实验的
host `run()`，不执行 kernel，也不制造模拟测量结果。

输出目录保存每个 kernel 的 ELF 和 `compile_report.json`，报告记录版本、目标、
源码/ELF 哈希、成功或错误。失败返回非零退出码。只能将实际报告中的通过称为
真实 ARTIQ 编译通过。

本次通过最小静态编译示例，以及 Rabi 的初始化与 49 个参数行、FlatTopGaussian 的
初始化与 4 个扫描点；故意把字符串传给 `delay_mu` 的负例返回 1，报告保留真实类型
错误。汇总为 `output/artiq_compile/verification_summary.json`。
这些结论只适用于报告记录的源码哈希、版本、目标和测试数据库，不能泛化为全部实验
或所有设备配置已验证。

该 ARTIQ 版本在 `core.host=None` 时的 `CommKernelDummy` 缺少 `close()`，
设备管理器会在编译后的清理阶段打印警告。上述成功样例仍返回 0 且 ELF 完整；
负例返回 1。保留该上游警告，不修改驱动或用 mock 替代真实编译器。
导出 manifest 中的默认 `not_verified` 不会被自动改写；单独的真实编译报告通过
源码哈希与导出关联。

`artiq_compile` 命令要求 `run()` 本身是 kernel，且不能使用 RPC；当前导出的
`run()` 是 host 扫描循环，因此这里使用逐 kernel 的 `Core.compile()` 入口。
`host=None` 只断开仪器通信，并不会把硬件系统自动变成仿真器。

| 检查 | 证明范围 |
| --- | --- |
| TaiyiDSL mock/event 验证 | 生成代码的逻辑事件与编译计划一致 |
| 真实 ARTIQ 编译 | 所选版本、驱动和 CPU 目标下的 kernel 类型、代码生成与链接 |
| 实机验收 | RTIO 提交预算、同步、实际寄存器行为、RF 与光学输出 |

编译成功不验证 host adapter、真实设备数据库、SPI 吞吐、RTIO underflow 或原子处
的门保真度。参考 [静态编译工具](https://m-labs.hk/artiq/manual/utilities.html#static-compiler)
和 [Core 源码](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/core.html)。
