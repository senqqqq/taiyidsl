# The ARTIQ toolchain, device database and RPC

TaiyiDSL runs on uv-managed CPython 3.12. Generate the experiment there and compile its kernel in the separate ARTIQ 9 environment. The Windows reference installation uses the official MSYS2 distribution in `.toolchains/artiq9`. The launcher sets PATH and TEMP/TMP for the child and restores the caller's environment. FPGA gateware is managed separately.

## Reproducing the reference installation

The [M-Labs installation instructions](https://m-labs.hk/artiq/manual/installing.html) describe supported platforms. The reference Windows installer was [build 202416](https://nixbld.m-labs.hk/build/202416), `artiq-msys2-x86_64-9.9664+071b41b.exe`, 457787346 bytes, SHA-256 `de60e9b448f31fcb925993bfc3c51630cb8fcf562eaae32fa73be6db8c9effce`. That environment contained ARTIQ 9.9664+071b41b, Python 3.14.5, llvmlite 0.47.0 and LLD 22.1.4. Store downloads and install receipts outside Git. Check the installed tools directly:

```powershell
.\scripts\artiq-python.ps1 -m artiq.frontend.artiq_compile --version
.\scripts\artiq-python.ps1 -m artiq.frontend.artiq_run --help
```

## What supplies the device database?

The launcher or ARTIQ master loads `device_db.py`. A generated experiment obtains named drivers with `get_device()`. The exported manifest identifies the devices it needs. Provide the laboratory database matching the gateware, core address, RTIO channels, SPI/CPLD/DDS wiring, clock and driver constructor parameters. A logical name alone cannot establish those values. `LabConfig` owns logical routing, resource declarations and timing contracts. `OpticsConfig` and `Qubit` own optical working points and qubit calibrations. See [the ARTIQ device database](https://m-labs.hk/artiq/manual/environment.html#device-database).

## Host and core calls

A kernel may use RPC to ask the host to run ordinary Python. Synchronous RPC waits for the result. Asynchronous RPC still incurs communication and queuing overhead. Account for these costs at the appropriate experiment boundary. The generated host scan loop calls the point kernel and then calls the result adapter directly on the host. The latter is an ordinary Python call. See [kernel and RPC semantics](https://m-labs.hk/artiq/manual/core_language_reference.html).

## Compile without instruments

```powershell
uv run --locked python examples/offline_imaging_workflow.py
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/offline_imaging/artiq --target rv32g --output output/artiq_compile/offline_imaging
```

Select the actual CPU target. The reference command explicitly uses `rv32g`. Other supported choices include `rv32ima` and `cortexa9`. The helper creates `device_db_compile_only.py` with real driver types, placeholder wiring and `core.host=None`. It invokes `Core.compile()` for initialization and each exported kernel/argument row. This avoids running the host loop and works for exported programs whose `run()` is a host method. The ordinary `artiq_compile` CLI expects a kernel `run()` and restrictions on RPC.

Each ELF and `compile_report.json` identify the source hash, target, toolchain and outcome. A failing compilation exits nonzero. ARTIQ's disconnected `CommKernelDummy` in the reference version may emit a cleanup warning about `close()`. Inspect both the exit status and ELF/report. Export manifests keep their original evidence level. Link the separate compile result by its source hash.

Mock/event checks compare the generated digital program with the plan. Real compilation checks typing, code generation and linking for a specified driver stack. Commissioning must still measure submission capacity, synchronization, register and RF behavior, and optical output. See the [static compiler](https://m-labs.hk/artiq/manual/utilities.html#static-compiler) and [Core implementation](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/core.html).
