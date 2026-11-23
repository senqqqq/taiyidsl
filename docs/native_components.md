# Compose native ARTIQ components

A `NativeModule` declares an existing ARTIQ class. A `NativeStage` names a callable method and its timing boundary. A `NativeProgram` composes independent MOT, cooling, state-preparation and readout objects. Each component has its own `HasEnvironment` instance. The four fixture classes in the example demonstrate TTL timing. Real preparation and detector implementations live in the laboratory's package.

```powershell
uv run --locked python -m examples.artiq_spectrum.native_components
```

## Register a class and call its stages

```python
from taiyidsl import NativeModule, NativeStage, NativeProgram, us

cooling = NativeModule.from_package(
    root=lab_source_root,
    entry="my_lab.cooling:Cooling",
    resources=("cooling_ttl",),
    build_kwargs={"mode": "green"},
    initialize="initialize_devices",
    after_shot="restore_idle",
    finalize="finish_session",
)
native = NativeProgram(
    modules={"cooling": cooling},
    stages={
        "GM_cooling": NativeStage(
            "cooling",
            "cool",
            timing="timed",
            duration=20 * us,
            end_boundary="falling edge of the final preparation TTL",
        ),
    },
)

with exp.realtime():
    native.GM_cooling(20 * us)
    delay(10 * us)
    with exp.triggered_sequence(group="qubit_awg"):
        gate_body()
```

Set `root` to the directory containing the top-level package, for example the package's `src/` directory. The factory discovers static local imports and package initializers. Use `source_overrides={"existing_lab.base": existing_base_path}` for reviewed dependencies outside that root. For a special package layout, supply a complete reviewed source map using `NativeModule(module=..., class_name=..., sources=..., ...)`. Missing entries, unresolved relative imports and dynamic loaders require correction.

Source discovery and compilation read files and ASTs without executing the laboratory classes. In the ARTIQ environment the exported program verifies file hashes and module origins before import. Use the standard `HasEnvironment` or `EnvExperiment` constructor. Build should bind hardware names and static host settings. Use a small laboratory adapter class to expose an existing kernel under the required interface.

Bind stage keyword arguments to the declared native signature; generated calls use positional arguments. The entry accepts fixed parameters with statically readable literal defaults. Variadic or keyword-only arguments are unsupported. Scalar values, lists, units and sweep parameters can enter a stage. `build_kwargs` accepts fixed finite serializable values.

## Timing and ownership

A shot has `prefix* → timed* → DSL control → suffix*`. Native calls are direct children of realtime. A variable-duration prefix may restore slack. After all prefixes the composer restores slack, establishes T0 and executes the timed stages in order. Exact timed and suffix stages check their returned cursor. Their reachable methods must avoid reset, break_realtime, blocking waits, RPC and dynamic calls. A requested gap after the last timed stage defines when the control body may begin. A trigger or preload that intrudes into opaque native time is rejected. Declare whether the preparation end boundary includes the analog settling time required in the laboratory.

| Hook | Lifetime |
| --- | --- |
| build | Once per native session, with static build arguments |
| host_prepare | Once when explicitly selected |
| initialize | Once before shots, with core reset owned by the root |
| after_shot | After each completed shot |
| finalize | Once at session end |

The program calls selected hooks in module declaration order, or in the explicit tuple order supplied for a hook. Undeclared prepare/run/analyze methods stay inactive. Failure during initialization or finalization invalidates the context. `GeneratedExperiment(managers, native_context=context)` binds subsequent chunks to the same component instances and once flags. `initialize_native_once()` and `finalize_native_session()` bracket the session. Context identity includes program fingerprint, core identity and completion state. Component host parameters use names such as `mot.duration` and `cooling.duration`. Dataset names remain the laboratory's responsibility.

```python
shared_resources = {"preparation_ttl": ("mot", "cooling", "state")}
```

The core is shared. For each other shared resource, name every participant and one selected initialization owner. The host inspects alias and bus identities after binding actual drivers. Each component must establish the state it uses. Native resources and compiled signals must remain disjoint where required, particularly the AWG trigger line.

Before calling a stage, the composer supplies T0, the physical base and effective terminal frame state. A phase-sensitive stage declares `phase_sensitive=True` and reads `self.native_terminal_f_<frame>`. This contains the full frame including virtual Z. Names that cannot be Python identifiers use stable hex-encoded attribute names. See `control_attributes.terminal_frames` in the export. Reserve those attributes for the composer.

## Verify the component

The existing `NativeKernelPackage` and `package.kernel()` interface remains usable. Its `finalize_method` runs once per shot. The GreenMOT fixture depends on that lifecycle. The multi-component example can be compiled using the disconnected ARTIQ environment:

```powershell
.\scripts\artiq-python.ps1 scripts/compile_native_artiq.py `
  output/artiq_spectrum/native_components/artiq `
  --output output/native_components/artiq_compile --target rv32g `
  --extra-device-db examples/artiq_spectrum/native_stages/device_db_offline.json `
  --module-root examples/artiq_spectrum
```

This compiles the initialization kernel, point kernel and session finalizer. Check source audit, offline call traces, real compiler receipts and physical device acceptance separately.
