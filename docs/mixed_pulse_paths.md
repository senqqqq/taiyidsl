# Combining Square, ASF RAM and POWASF RAM on separate DDS channels

One shot may combine timed single-tone Square (buffered profile and IO_UPDATE), resident ASF RAM (envelope plus independent POW register) and resident POWASF RAM (packed amplitude and phase with pipeline-compensating phase prefixes). Each DDS has one playback mode for the shot. All paths share the same physical event timeline. Load and prepare all channels while RF is off, stage NCO clear release and commit one epoch per participating DDS as scheduled. Independent SPI buses can prepare concurrently.

For a timed Square and a shaped POWASF envelope mapped to separate DDSs:

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

The two gates start at the same logical boundary. The Square ends after 200 ns and the shaped pulse after 800 ns. Parallel timing comes from the explicit parallel scope and physical resource mapping.

## Urukul interface revision

Rev8 and Rev9 denote Urukul **CPLD interface protocols** `proto_rev=0x08/0x09` (`ProtoRev8/ProtoRev9`). Hardware and running firmware determine the protocol and therefore the register layout and SPI transactions. Match `Device.urukul_proto_rev`, the device database and, for POWASF, `ram_profile_control` to the deployed board. Check the actual database, firmware inventory and version observed during normal initialization. The demonstration device databases represent offline configurations. [Urukul driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)

| Driver protocol | `set_profile` cursor advance at 8 ns coarse, divider 2 | Selection |
|---|---:|---|
| Rev8 | 416 ns | Four channels share profile pins |
| Rev9 | 896 ns | Independent channel fields, longer transfer |

These are interface costs. During a previous ARTIQ 9.9664 offline build, mixing Rev8 and Rev9 CPLD objects within one kernel produced a type unification error on `CPLD.version`. Use the real protocol for every DDS. Cross-DDS playback-mode support and same-kernel mixed protocol support have distinct requirements.

## Short gates and sustained selection throughput

| 200 ns case | Contract |
|---|---|
| One preselected gate in a shot | Offline-supported on Rev8 and Rev9. Selection can happen in preroll. |
| Repeated resident profile with the same incoming phase prefix | Retrigger with IO_UPDATE. The example starts on X and repeats X/X/X/X without a frequency, amplitude or axis change. |
| X→Y→X needing a different profile every 200 ns | Reject. The 416/896 ns calls exceed the interval. |
| Repeated logical Y starting from X | The first X→Y and later Y→Y prefixes can differ, requiring different resident variants. |

Repreparing a gate between shots gives a different throughput requirement from sustained axis changes in one shot. These limits also apply across channels:

- Same-bus transfers serialize. Separate buses can overlap with absolute-time RTIO edges.
- Rev8 shares profile pins. The current contract requires exclusive active DDS use on the relevant bus for that selection.
- Active RAM sharing IO_UPDATE with another DDS is rejected because a shared edge could retrigger the RAM or commit unwanted state.
- Changing one DDS between Square, ASF RAM and POWASF in a shot would change mode, FTW source and startup state. The compiler requires fixed mode per DDS per shot.
- A native Square update requiring a 1248 ns profile write cannot sustain different profiles every 800 ns.
- ASF RAM and its independent POW register share the commit. Strict aligned envelope/phase changes still require matching latencies.

Positive examples use independent SPI and IO_UPDATE groups on **one RTIO core**. Logical channel names do not establish independence.

## Scenarios and reproduction

`examples/mixed_pulse_paths.py` covers these combinations:

- Consecutive 800 ns POWASF X/Y in parallel with an 800 ns Square.
- A single 200 ns gate and repeated 800 ns gates on each of the three paths.
- A 200 ns Square on another DDS opening at 400 ns and closing at 600 ns while the first channel's SPI selects its 800 ns POWASF profile.
- Four identical 200 ns Rev9 POWASF gates in parallel with one 800 ns Square.
- A terminal hardware Rz on each path after three 800 ns gates.
- A standalone/terminal Rz affecting both drives of the same qubit, with Raman mapped to POWASF and RF to Square.

The repeated Square uses identical parameters and can form a single continuous RF interval. The ASF example deliberately uses unequal `L_env=112 ns` and `L_pow=80 ns`. It demonstrates same-axis replay and terminal RF-off Rz. All example output latencies require deployment-specific measurement.

```powershell
uv run --locked python examples/mixed_pulse_paths.py
uv run --locked pytest tests/test_mixed_pulse_paths.py --basetemp=.test-tmp-pytest
.\scripts\artiq-python.ps1 scripts/verify_mixed_polar_driver.py output/mixed_pulse_paths/mixed_three_paths_200
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/mixed_pulse_paths/mixed_three_paths_200 --target rv32g --output output/mixed_pulse_paths/mixed_three_paths_200/artiq_compile
```

`output/mixed_pulse_paths/verification_summary.json` records hashes and results for previous real-driver virtual RTIO checks and ARTIQ 9.9664/rv32g builds, including table and unrolled cases, a 1024×800 ns X/Y library replay and the heterogeneous-protocol rejection. Reverify when source or toolchain identity changes. Verify clocks, RF waveforms and sustained throughput on the target separately.
