# Trigger and replay a Spectrum AWG sequence

Run `uv run --locked python -m examples.spectrum_awg_pair --cards 2` to write a manifest and generated ARTIQ trigger program under `output/spectrum_awg_pair/`. Repeat with `--cards 1` to use one output. `examples/spectrum_awg_pair.py` shows the wiring, card contracts and experiment. All addresses and latencies here are simulated.

The example plays two 400 ns pulses separated by 800 ns. At 1.25 GS/s each pulse occupies 500 samples and the second starts 1,500 samples after the first. The ARTIQ TTL rising edges are scheduled 512 ns before the logical start. The two cards declare trigger latencies of 500 and 484 samples. Their initial zero pads are 140 and 156 samples, placing both first pulses at sample 640 relative to the rising edges. The default carrier reference is the sequence entry.

Read `points[0].groups[0].trigger_routes` in `manifest.json` for the TTL edges, then `cards.A.program` for pulse samples, finite segments and loop counts. `banks[0].groups[0].cards.A.program` contains the point entry, segment slots and steps. Each path ends independently. Identical packed bytes can share a slot across entries. The card's memory limit applies to equal-sized slots, including alignment and enabled channel count. The compiler rejects a program that exceeds the available capacity.

`uv run --locked pytest tests/test_spectrum_awg_pair.py` checks the edges, zero padding, packed first sample, shared bank entries and capacity failure. Offline ARTIQ replay checks the generated trigger commands. Physical trigger skew and analog amplitude require a measurement on the configured instruments.
