"""Project configuration, not a Spectrum SDK database; no hardware is opened."""

spec_db = {
    "A": {
        "address": "/dev/spcm0",  # local PCIe card identifier, not an IP address
        "serial_number": "SIM_A",
        "model": "M4i.6631-x8",
        "active_channels": [0],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "output_range_mv": 500,
        "trigger_latency_samples": 500,  # illustrative, NOT measured
    },
    "B": {
        "address": "/dev/spcm1",
        "serial_number": "SIM_B",
        "model": "M4i.6631-x8",
        "active_channels": [0, 1],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "output_range_mv": 500,
        "trigger_latency_samples": 520,  # illustrative, NOT measured
    },
}
