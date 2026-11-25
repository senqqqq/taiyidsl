# From device databases to experiment signals

`DeviceSetup.from_descriptor()` reads configuration offline. It accepts ARTIQ's standard `device_db` dictionary and TaiyiDSL's `spec_db` dictionary for Spectrum devices. Each can be supplied directly or as a declarative Python file. The reader supports literals, constant references and basic arithmetic. It never executes imports, functions or loops. For a dynamic database, evaluate it in the laboratory's trusted configuration process and pass the resulting dictionary. Reading a descriptor requires neither ARTIQ nor `spcm`.

For a bench with one Spectrum card, use the [standalone descriptor](spectrum_standalone.md) with `spec_db` and an internal clock.

## Reuse an existing ARTIQ installation

Pass the lab's `device_db.py` path and select device names in the descriptor. The core's address stays in `device_db["core"]["arguments"]["host"]`, the TTL's RTIO channel stays in `arguments.channel`, and the database supplies the DDS/SPI/CPLD topology. The DSL and the native GreenMOT package use the same database snapshot. The database can include devices used only by native programs.

If `ttl6` and `ttl7` are free and wired to cards A and B, use `"triggers": {"A": "ttl6", "B": "ttl7"}`. The declarative database can also give these existing outputs aliases, for example `"awg_trigger_a": "ttl6"` and `"awg_trigger_b": "ttl7"`. An alias keeps the original RTIO channel. Check the lab's wiring and existing native programs for shared lines before assigning them. The host, names and channel numbers in an example describe only its fixture.

## Addresses, wiring and aliases

ARTIQ uses the core's network address and a separate RTIO channel number for an output. Spectrum uses the driver's enumerated card identifier or a local serial-number lookup. For instance, `/dev/spcm0` selects a PCIe device. Its DAC output number belongs in `channels.*.channel`. A remote identifier such as `TCPIP::192.0.2.20::inst0::INSTR` requires a remote device exposed by the driver. Wire the trigger TTL to EXT0, the reference clock to its own input, and the signal to the chosen DAC port. The descriptor records the intended connections. See [Spectrum's connection examples](https://github.com/SpectrumInstrumentation/spcm#connect-to-a-device).

```python
spec_db = {
    "A": {
        "address": "/dev/spcm0",  # Example driver identifier. Substitute the installed card.
        "serial_number": "12345",
        "active_channels": [0, 1],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "output_range_mv": 500,
    },
    "B": {
        # Without address, select by serial_number. A/B names do not imply device order.
        "serial_number": "12346",
        "active_channels": [0, 1],
    },
}

descriptor = {
    "name": "laboratory",
    "backend": "artiq-spectrum",
    "instruments": {
        "artiq": {"kind": "artiq", "device": "core"},
        "A": {"kind": "spectrum", "device": "A"},
        "B": {"kind": "spectrum", "device": "B"},
    },
    "channels": {
        "nuclear_rf": {"instrument": "A", "channel": 0},
        "clock": {"instrument": "B", "channel": 0},
        "camera": {"instrument": "artiq", "device": "camera_ttl"},
        "awg_trigger_a": {"instrument": "artiq", "device": "awg_trigger_a"},
        "awg_trigger_b": {"instrument": "artiq", "device": "awg_trigger_b"},
    },
    "sync_groups": {
        "qubit_awg": {
            "cards": ["A", "B"],
            "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b"},
        },
    },
    "signals": {
        "q0": {"rf": "nuclear_rf", "clock": "clock"},
        "camera": "camera",  # A standalone signal can have its own name.
    },
}
```

`signals["q0"]["rf"]` expands to `q0.rf` and matches `Qubit(signals={"rf": "q0.rf"})`. The flat spelling `"q0.rf": "nuclear_rf"` also works. Each logical name must be unique across both forms. Grouping accepts a single `qubit → role → channel` level. Group and role names in that form cannot contain a dot. Standalone signals such as `camera` can remain top-level entries.

Group triggers can name a declared TTL channel or a device directly in `device_db`. A channel and device with the same name must resolve to the same endpoint. Trigger lines belong to the synchronization group and do not need a qubit mapping. The compiler owns these lines while the group is active.

```python
descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
descriptor["signals"]["q1"] = {"rf": "nuclear_rf"}
```

This wiring sends `q0.rf` and `q1.rf` to one physical field. To choose the calibration explicitly, use `ops.global_x90(q0, q1, calibration=q0)`. Independent addressing requires distinct physical ports, declared in `channels` and mapped separately.

Each `instruments.*.device` refers to its database entry, and each physical endpoint is declared once. ARTIQ channels reference existing device names. The reader resolves aliases and derives AD9910 SPI, CPLD and system-clock properties from the database. It uses the ARTIQ 9 driver's `refclk/clk_div/pll_en` rules and rejects the unsupported `pll_n` constructor argument. A core alias is allowed, and all ARTIQ instruments in the descriptor must resolve to the same core. Different names cannot claim the same RTIO output. TTLs and AD9910s are supported for DSL playback. Native packages can use other devices. Optional `channels.*.timing` entries supply output delays and other timing declarations while preserving the driver topology.

Spectrum `address` can select a local driver device or a remote VISA identifier. After opening the card the adapter checks `serial_number`. Settings use `SpectrumCard` field names for sample rate, reference clock, enabled channels and output range. The default hybrid policy requires M4i.6631-x8, 1.25 GS/s and a 125 MHz external reference. Defaults are software settings. Group `timing` gives compile-time constraints and `calibration` supplies measurements. Use singular `trigger` for a shared trigger line.

For a global field, put `sharing="global"` on the channel and map every covered logical signal to its alias. `setup.connections()` returns signal-to-port rows. `setup.trigger_connections()` returns TTL-to-card rows. Both return copies.

```python
setup = DeviceSetup.from_descriptor(
    descriptor, device_db="C:/lab/config/device_db.py", spec_db=spec_db
)
lab = LabConfig(setup, qubits=[q0], emulation=True)
compiled = lab.compile(exp)
```

Qubit drive frequencies and gate amplitudes affect pulse compilation. Sample rate, reference clock and analog range belong to the hardware configuration. The lab checks the wiring identity of a registered qubit against the experiment and compiles with the calibration captured in that experiment. The older `LabConfig(setup, signal_map, backend)` constructor and lab JSON still support offline builds. Their serialized compile settings do not contain the full databases needed to connect. Descriptor-based products record the descriptor and database sources.

## Inspect the resolved configuration

```python
from pprint import pprint

pprint(setup.describe())
pprint(lab.describe())
```

| `setup.describe()` field | Contents |
| --- | --- |
| `name`, `backend` | Laboratory name and internal backend name |
| `databases` | Source paths or dictionary origins, with content fingerprints |
| `instruments` | Database entries, resolved connection selectors and effective settings |
| `channels` | Laboratory names and declared physical endpoints |
| `sync_groups` | Card membership, TTL wiring, declared timing and calibration |
| `signals` | The input signal map, grouped or flat |
| `connections` | One row per logical signal and resolved instrument/port |
| `trigger_connections` | TTL-to-card rows after alias resolution |

For example, `instruments["artiq"]["connection"]` gives the core driver class and configured host. `instruments["A"]["connection"]` gives `spcm.Card` and the configured `address`, or the actual `serial_number` selector when address is omitted. Spectrum `settings` includes defaults for model, clock, rate, enabled channels and output range.

`lab.describe()` also reports `backend`, `emulation`, `artiq_python`, qubit snapshots and the derived `signal_map`. These offline views return independent copies. `setup.to_dict()` and `lab.to_dict()` retain their compile-configuration serialization format.

## From descriptor to driver

1. `from_descriptor()` validates the card declarations, converts them to `SpectrumCard` constraints and stores connection selectors separately.
2. `LabConfig.compile()` checks sample grids, capacity and timing, then generates DAC samples. Signal and channel mappings determine their destination card and port.
3. `connect()` starts the ARTIQ environment. `SpcmAdapter` opens an official `spcm.Card` by address or serial number and checks identity and clock configuration.
4. `SpectrumRunner` preflights every card. The adapter's `configure()` and `upload()` methods use `Channels`, `Trigger` and `Sequence` to configure outputs, triggering and sequences and transfer samples. All cards must report ready before the ARTIQ trigger kernel starts.

The DSL controls phase, segmentation, the common timeline and trigger scheduling. The driver operates the hardware. Connecting and running check the existing clock configuration. Changing clocks requires new synchronization calibration.

## One AWG on the bench without a shared reference

For a single `artiq_spectrum` AWG triggered by an ARTIQ TTL into EXT0, set
`spec_db["A"]["clock_policy"] = "internal_pll_bench"` **before** creating the
setup and compiling. Do not supply an external `reference_clock_hz` or a
calibration profile. The card must already be set to internal PLL at 1.25 GS/s;
make any necessary change separately, with the card stopped and exclusively
owned. Connect and run verify the selected mode and rate; neither writes clocks
nor falls back when they differ. A stale external-reference register is unused.
Use the usual `with lab.connect() as session: receipt = session.run(compiled)`.

The TTL starts a finite replay; sample spacing and relative phases within that
replay come from the card, not the core. Do not use this for shot-wide phase,
multiple cards, DDS transport or calibrated synchronization. A successful card
receipt says `output_basis="setpoints_only"`, `shared_reference=false`, and
`analog_delay_gain_absolute_phase="not_verified"`. Arm/readiness, completion,
waveform integrity and identity checks still apply. Removing the option restores
the strict policy on **recompilation**, not on an already compiled shot. The
separate `spectrum` backend already uses an internal PLL with software triggers;
it does not exercise the ARTIQ TTL wire.

## Connect and run

Constructing `LabConfig(..., emulation=False, artiq_python=...)` still works offline. `lab.connect()` starts the separate ARTIQ process, reads the pinned `device_db`, connects the core through ARTIQ's DeviceManager and opens Spectrum cards through `spcm`. Select the worker with `artiq_python` or `TAIYIDSL_ARTIQ_PYTHON`; only Windows defaults to `.toolchains/artiq9/clang64/bin/python.exe`. See the [Linux Nix worker and offline diagnostic](nix_hardware_worker.md). Install `spcm` and its native driver in the selected hardware environment.

```python
with lab.connect(clock_session_ids=verified_clock_sessions) as session:
    receipt = session.run(compiled)
```

A new connection gets a fresh clock-session ID by default. Supply an existing ID only if the lab continuously monitored the clock and confirmed that it did not relock. Under the default external-reference policy, run requires measured calibration bound to the correct hardware fingerprint and live clock session. Copying the ID from a calibration file or changing the emulation flag does not establish those conditions. Reconfigure and recalibrate explicitly after a clock change.

`session.run(compiled)` exports and runs the same compiled object. The runner preflights, uploads, waits for readiness, runs one top-level kernel, then waits for completion. Exiting the context closes the devices. Receipts and workspaces live under `output/hardware_sessions/`. Worker and native-code output goes to `worker.log`. The native package uses its declared ARTIQ build-argument defaults. The lab must supply any required argument without a default. The session neither prompts through the ARTIQ GUI nor creates a DatasetDB.

The hardware session returns an **execution receipt**. A GreenMOT camera TTL is a timing event. This session has no detector adapter and rejects compiled experiments with DSL acquisitions. Actual images require a laboratory detector and a result adapter through a supported execution entry point. A failed hardware attempt is never retried automatically.
