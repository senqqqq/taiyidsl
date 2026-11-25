# Trigger and replay a Spectrum AWG sequence

Run `uv run --locked python -m examples.spectrum_awg_pair --cards 2` to write a manifest and generated ARTIQ trigger program under `output/spectrum_awg_pair/`. Repeat with `--cards 1` to use one output. `examples/spectrum_awg_pair.py` shows the wiring, card contracts and experiment. All addresses and latencies here are simulated.

The example plays two 400 ns pulses separated by 800 ns. At 1.25 GS/s each pulse occupies 500 samples and the second starts 1,500 samples after the first. The ARTIQ TTL rising edges are scheduled 512 ns before the logical start. The two cards declare trigger latencies of 500 and 484 samples. Their initial zero pads are 140 and 156 samples, placing both first pulses at sample 640 relative to the rising edges. The default carrier reference is the sequence entry.

Read `points[0].groups[0].trigger_routes` in `manifest.json` for the TTL edges, then `cards.A.program` for pulse samples, finite segments and loop counts. `banks[0].groups[0].cards.A.program` contains the point entry, segment slots and steps. Each path ends independently. Identical packed bytes can share a slot across entries. The card's memory limit applies to equal-sized slots, including alignment and enabled channel count. The compiler rejects a program that exceeds the available capacity.

`uv run --locked pytest tests/test_spectrum_awg_pair.py` checks the edges, zero padding, packed first sample, shared bank entries and capacity failure. Offline ARTIQ replay checks the generated trigger commands. Physical trigger skew and analog amplitude require a measurement on the configured instruments.

## When the driver validates the start step

On the M4i.6631-x8, a stopped card in `SPC_REP_STD_SEQUENCE` still validates its selected entry when `M2CMD_CARD_WRITESETUP` is issued. Selecting step 0 does not define it. `Sequence check: start step (0) not valid` (driver error 267) is a setup-order diagnostic, not an instruction to change the card serial, oscillator or trigger mask.

- Stage card, channel and trigger settings without committing an empty sequence. Partition the bank, DMA its segments, write the step table, select the defined start step, **then** commit. An interrupted bank replacement or failed entry change must not leave the previous entry armable.
- The hybrid runner retains the declared clock source and EXT0 TTL. Once every participating AWG is uploaded and armed and every participating adapter is ready, ARTIQ can issue its planned trigger. No software force trigger or PLL relock is part of normal hybrid AWG setup. Switching to a different *valid resident* entry can commit the selection without DMA.
- To apply a clock change for explicit calibration, or to commit amplifier-off settings when relinquishing an AWG, use a stopped, non-sequence mode with a legal memory length. Do not start that mode or validate a stale step table. After any clock-change attempt, re-establish and measure the physical configuration before trusting old phase corrections.
- Standalone AWG uses the same upload and release rules, but its internal clock and software launch are intentionally different. Native DDS uses its command queue, not an AWG step table. Releasing finite AWG banks normally must not break a live DDS hold.

The Spectrum M4i.66xx manual documents segment allocation and the start-step register in *Sequence Replay Mode* (tables 136 and 138). The mocked SDK tests in `tests/test_spectrum_runtime.py` reject commits to undefined entries. Passing those checks is not a measurement of hardware timing or output.
