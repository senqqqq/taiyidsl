# Write a native ARTIQ component and declare its timing

The hybrid notebook generator extracts the two Python cells below. The notebook already defines `SESSION` (a `Path` to the output directory), `lab`, `q0`, `q1` and `ops`. Calling `native.GM_cooling(...)` while building the experiment records a future native call. The TTLs in this tutorial illustrate component boundaries. Building the notebook does not run a kernel or connect to a core device.

## Two ways to integrate native code

| Example | Implementation | Lifecycle |
|---|---|---|
| `examples/artiq_spectrum/greenmot_ramsey.py` | `GreenMOTAdapter` uses `NativeKernelPackage` to integrate one native class into the generated program. | Retains the original initialization and methods. Its `finalize_method` runs after each shot. |
| `examples/artiq_spectrum/native_components.py` | `NativeProgram` composes four `NativeModule` instances for MOT, cooling, state preparation and readout. | Four separate `HasEnvironment` instances share managers and a core. `initialize` runs once per session, `after_shot` once per shot and `finalize` once at session end. |

The four TTL-backed modules in `native_stages/` are teaching fixtures. The GreenMOT example also uses a fixture by default. Pass an external `_GreenMOTbase.py` explicitly to audit actual upstream source. In both approaches a single top-level point kernel calls the native methods and generated control segment on the core.

The real GreenMOT preparation already includes cooling. The adapter extracts the final three statements from `prepare_GreenMOT()` and checks their concatenation with the prefix against the upstream AST statement by statement. The tail ends with a Zotino update. Advancing the cursor to the last rising LDAC edge gives a declared duration of `15 * ms + 16 * ns`. This boundary marks completion of the digital update. Declare analog settling separately. The tail requires a 1 ns machine unit and an 8 mu SPI reference period.

## Cell 1. Save the complete native source

The four classes share one `stages.py` so you can read their methods together. If you split them across modules, update the `sources` mapping. Keep import-time code free of experiment execution.

```python
import textwrap

source_root = SESSION / "native_sources"
package_dir = source_root / "tutorial_native"
package_dir.mkdir(parents=True, exist_ok=True)
(package_dir / "__init__.py").write_text(
    '"""Native tutorial package; imported only in the ARTIQ environment."""\n',
    encoding="utf-8",
)
native_source = textwrap.dedent("""\
from artiq.experiment import HasEnvironment, delay, kernel


class MOT(HasEnvironment):
    def build(self, prefix_duration=40e-6):
        self.setattr_device("core")
        self.setattr_device("tutorial_prepare")
        self.prefix_duration = prefix_duration

    @kernel
    def initialize(self):
        self.tutorial_prepare.off()

    @kernel
    def prepare(self):
        self.core.break_realtime()
        self.tutorial_prepare.on()
        delay(self.prefix_duration)

    @kernel
    def after_shot(self):
        self.tutorial_prepare.off()

    @kernel
    def finalize(self):
        self.tutorial_prepare.off()


class Cooling(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("tutorial_prepare")

    @kernel
    def GM_cooling(self, duration):
        self.tutorial_prepare.on()
        delay(duration)
        self.tutorial_prepare.off()


class StatePreparation(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("tutorial_prepare")

    @kernel
    def state_prep(self, duration):
        delay(duration)
        self.tutorial_prepare.off()


class Readout(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("tutorial_camera")
        self.last_frame = 0.0

    @kernel
    def initialize(self):
        self.tutorial_camera.off()

    @kernel
    def readout(self, duration):
        self.last_frame = self.native_terminal_f_q0
        self.tutorial_camera.on()
        delay(duration)
        self.tutorial_camera.off()
""")
(package_dir / "stages.py").write_text(native_source, encoding="utf-8")
```

Host-side `build` binds devices and static parameters. Keep instrument operations in the appropriate kernel. Use literal names such as `setattr_device("tutorial_prepare")` so the auditor can identify devices. Dynamic device registration in a loop or kernel-side `getattr` for devices or methods prevents this check. Inherit the standard constructor from `HasEnvironment` or `EnvExperiment`.

The teaching `StatePreparation` holds a TTL low for the declared interval. `Readout.last_frame` records the drive frame at the end of control on `q0`. A physical state preparation or phase-sensitive measurement requires an appropriate laboratory implementation. For other frame names, look up the generated attribute in `control_attributes.terminal_frames`.

## Cell 2. Declare sources, resources, stages and hooks

`source_root` names a directory. In `NativeModule.sources`, map each importable name to its file, including package `__init__.py` and every user source file on the call path. Declarations pin absolute paths and SHA-256 hashes. The notebook can declare the package without importing it or changing `sys.path`.

```python
import sys
from taiyidsl import NativeModule, NativeProgram, NativeStage, us

pinned_sources = {
    "tutorial_native": package_dir / "__init__.py",
    "tutorial_native.stages": package_dir / "stages.py",
}
modules = {
    "mot": NativeModule(
        module="tutorial_native.stages",
        class_name="MOT",
        sources=pinned_sources,
        resources=("tutorial_prepare",),
        build_kwargs={"prefix_duration": 40e-6},
        initialize="initialize",
        after_shot="after_shot",
        finalize="finalize",
    ),
    "cooling": NativeModule(
        module="tutorial_native.stages",
        class_name="Cooling",
        sources=pinned_sources,
        resources=("tutorial_prepare",),
    ),
    "state": NativeModule(
        module="tutorial_native.stages",
        class_name="StatePreparation",
        sources=pinned_sources,
        resources=("tutorial_prepare",),
    ),
    "readout": NativeModule(
        module="tutorial_native.stages",
        class_name="Readout",
        sources=pinned_sources,
        resources=("tutorial_camera",),
        initialize="initialize",
    ),
}
native = NativeProgram(
    modules=modules,
    stages={
        "prepare_MOT": NativeStage("mot", "prepare", timing="prefix"),
        "GM_cooling": NativeStage(
            "cooling",
            "GM_cooling",
            timing="timed",
            duration=20 * us,
            end_boundary="tutorial_prepare falling edge",
        ),
        "state_prep": NativeStage(
            "state",
            "state_prep",
            timing="timed",
            duration=8 * us,
            end_boundary="end of the declared low-level hold",
        ),
        "readout": NativeStage(
            "readout",
            "readout",
            timing="suffix",
            duration=10 * us,
            phase_sensitive=True,
            end_boundary="tutorial_camera falling edge",
        ),
    },
    shared_resources={"tutorial_prepare": ("mot", "cooling", "state")},
)
assert "tutorial_native.stages" not in sys.modules
```

- Declare the resources each module uses. The core is shared implicitly. Keep native devices and buses separate from outputs used by generated control, including the AWG trigger TTL. Preflight resolves device aliases to their identities.
- Three preparation modules share `tutorial_prepare`. List all participants explicitly. Only `mot` owns its initialization. Selecting two initializers for that shared resource raises an error. Readout initializes its own camera TTL. Each component maintains its own software cache.
- Put static serializable configuration in `build_kwargs`. Stage arguments may be scalars, lists, quantities or sweep values. Declare exact duration separately. Here `native.GM_cooling(20 * us)` matches the declared `20 * us`.
- Use a fixed positional signature. Writing `native.GM_cooling(duration=20 * us)` binds the keyword and emits a static positional call. The adapter excludes variadic and keyword-only parameters. Defaults must be AST-readable literals.
- To select a host preparation hook, set `host_prepare` to its method name. Ordinary `prepare/run/analyze` methods run only if selected as supported hooks. Host preparation and build execute trusted host code. Program-level `initialize/after_shot/finalize/host_prepare` tuples can select and order components. The default follows module declaration order.

## Call the stages from the main experiment

Reuse the notebook's `lab`, `q0`, `q1` and `ops` so the configured signal names agree with terminal frame attributes. The notebook builds, compiles and exports the complete experiment.

In the direct body of `realtime`, call `native.prepare_MOT()`, `native.GM_cooling(20 * us)` and `native.state_prep(8 * us)`. Leave an explicit gap for triggering, add the control segment on `q0`, then call `native.readout(10 * us)`. Native calls must be direct children of the realtime block. Sections, parallel branches, repeats, cases, triggered sequences and gaps between DSL gates are unsupported native call sites.

The order is `prefix* → recover slack and establish T0 → timed* → DSL control → suffix*`. A prefix can use `break_realtime`. Its internal 40 µs delay does not give the whole prefix a fixed duration. On return from every timed or suffix stage, the generated kernel checks the actual cursor difference against the declared duration. It raises on both underrun and overrun, without inserting a delay. A static audit cannot measure the runtime duration.

The timed/suffix call graph excludes resets, `break_realtime`, blocking waits, RPCs and dynamic calls. The root program owns resets for the new component lifecycle, including initialization, after-shot and finalization. The older GreenMOT package retains its initialization convention. Leave enough explicit separation for AWG pretrigger and preload. The compiler rejects a schedule that would put them inside an opaque timed native stage.

The default sequence carrier reference starts at entry to `triggered_sequence`. Earlier preparation and gaps precede that reference. Physical initialization of the quantum state is a separate operation. A phase-sensitive readout can use the full persistent drive frame `native_terminal_f_q0`, which already includes the Rz contribution.

## Import paths and disconnected compilation

Construction and export read the AST and hashes. Export leaves the native package at its existing location. When loading a generated program in an ARTIQ process, make the parent directory of `tutorial_native` importable.

| Purpose | Path under the notebook session |
|---|---|
| Native import root, also `--module-root` | `SESSION / "native_sources"` |
| Native source package | `SESSION / "native_sources/tutorial_native"` |
| Hybrid Ramsey export | `SESSION / "ramsey_export"` |
| Export argument to `compile_native_artiq.py` | `SESSION / "ramsey_export/artiq"` |
| `--extra-device-db` | `SESSION / "extra_device_db.json"` |
| `--output` | `SESSION / "artiq_compile_ramsey"` |

Save the following disconnected compilation devices to `extra_device_db.json` under this session, in UTF-8 without a BOM. Channels 20 and 21 are placeholders. Use the laboratory wiring when preparing a real deployment.

```json
{
  "tutorial_prepare": {
    "type": "local", "module": "artiq.coredevice.ttl", "class": "TTLOut",
    "arguments": {"channel": 20}
  },
  "tutorial_camera": {
    "type": "local", "module": "artiq.coredevice.ttl", "class": "TTLOut",
    "arguments": {"channel": 21}
  }
}
```

Prepare the independent [ARTIQ toolchain](../artiq_toolchain.md). From the project root run the following command with the session printed by the notebook.

```powershell
# Replace the placeholder with the session directory printed by the first cell.
$lesson = 'C:/AIproject/taiyidsl/output/tutorial_from_scratch/runs/<session-directory>'
.\scripts\artiq-python.ps1 scripts/compile_native_artiq.py `
  "$lesson/ramsey_export/artiq" `
  --output "$lesson/artiq_compile_ramsey" `
  --target rv32g --point 0 `
  --extra-device-db "$lesson/extra_device_db.json" `
  --module-root "$lesson/native_sources"
```

For RB, look up chunk directories and local point numbers in `rb_artifact/store.json`. For example, local point 5 in `chunks/1/artiq` may be global point 23. Source and ELF hashes in the compile report identify only that exact artifact. Recheck them after regeneration.

`scripts/artiq-python.ps1` runs `scripts/compile_native_artiq.py` in an independent ARTIQ process. The script makes the module root available locally, builds a device database with `host=None` and calls `Core.compile` to save an ELF. Constructing the experiment imports user modules and executes trusted host build code, so keep build free of instrument actions.

The loader checks pinned hashes, import resolution and module caches before importing a class. Reconstruct and export declarations after moving or editing sources. Use a fresh worker for stale or unverified caches. A laboratory launcher must also make the pinned module root available to its worker, for example through an installed laboratory package or a process-local import path.

Record AST/hash audit and export, mock traces, real ARTIQ compilation, instrument timing and measured physical output separately. This tutorial runs offline.
