# Laboratory extension example

The `src/my_lab` package can be copied into a dedicated laboratory repository. It illustrates the boundary between the general DSL and laboratory-owned native code, camera configuration and host adapters. Generated images are synthetic arrays. The example runs offline without a vendor SDK. The native camera TTL illustrates the component interface.

## Install and run

From the TaiyiDSL source checkout, install the locked library and the example package into the same interpreter.

```powershell
uv sync --locked
$labPython = uv run --no-sync python -c "import sys; print(sys.executable)"
uv pip install --python $labPython --no-deps -e examples/lab_extension
uv run --no-sync python -m my_lab.demo
```

On Linux or macOS:

```bash
uv sync --locked
lab_python="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
uv pip install --python "$lab_python" --no-deps -e examples/lab_extension
uv run --no-sync python -m my_lab.demo
```

The main project already provides `taiyidsl` from its lockfile, so install the example with `--no-deps`. Use `--no-sync` when running it to preserve the local extension outside the main lockfile. A real laboratory package should own its dependencies, private source or wheel configuration and lockfile. Device addresses, calibration and vendor SDK requirements belong to that package.

Each execution creates a `run_*` directory under `output/lab_extension/` with raw frames, observation commits and a SQLite journal. The summary and ordinary image archive update with the current demonstration. `--output <directory>` selects an alternate demonstration location.

## Source files

| File | Purpose |
|---|---|
| `src/my_lab/native.py` | ARTIQ camera-trigger class with explicit initialize/finalize methods |
| `src/my_lab/declarations.py` | Pin resources and source with `NativeModule.from_package` and expose a suffix stage via `NativeProgram` |
| `src/my_lab/camera.json` | Example ROI, background and classification threshold |
| `src/my_lab/configuration.py` | Result schema for native-triggered images |
| `src/my_lab/camera.py` | Fake camera, ordinary adapter and persistent adapter with recovery |
| `src/my_lab/demo.py` | Runnable host hook order and recovery example |

Importing `my_lab.declarations` leaves ARTIQ and `my_lab.native` unloaded. Install native code in the ARTIQ environment and the readout adapter in the acquisition environment. Install their respective third-party dependencies separately. The laboratory device database must declare `camera_trigger`.

## Use the adapters

Pass the native declaration explicitly into the experiment and call `native.readout(10 * us)` after the control block. The stage's declared 10 µs duration must match its arguments. Update the declaration and recompile when changing trigger settings. Camera exposure and transfer time need their own configuration. See [native components](../../docs/native_components.md).

For ordinary exported programs set `experiment.result_adapter = CameraAdapter(...)`. For persistent execution use `runner.run(artifact, result_adapter=PersistentCameraAdapter(...), run_store=store)`. See [readout adapters](../../docs/readout_adapters.md) for callback signatures and indices.

The demonstration writes journal states explicitly and marks them `simulated`. During hardware execution, `StreamingRunner` manages the state transitions. The adapter saves raw frames and returns an observation receipt. Close actual SDK connections in a context manager or `try/finally` because `finish` can be bypassed after an exception.

## Verification

The targeted software tests cover ordinary two-argument callbacks, empty versus missing observations, triggering before retrieval, and recovery when raw frames survive an interrupted commit. Recovery of that data avoids replaying the completed hardware shot. Validate the actual camera and run independent ARTIQ compilation when integrating the laboratory package.
