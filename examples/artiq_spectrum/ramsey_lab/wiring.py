"""Signal roles and physical wiring, separate from device addresses/calibration."""

descriptor = {
    "name": "ramsey_teaching_lab",
    "backend": "artiq-spectrum",
    "instruments": {
        "core": {"kind": "artiq", "device": "core"},
        "A": {"kind": "spectrum", "device": "A"},
        "B": {"kind": "spectrum", "device": "B"},
    },
    "channels": {
        "nuclear_rf": {"instrument": "A", "channel": 0},
        "clock": {"instrument": "B", "channel": 0},
        "rydberg": {"instrument": "B", "channel": 1},
        "camera": {"instrument": "core", "device": "ttl_Camera_Trig"},
        "awg_trigger_a": {"instrument": "core", "device": "awg_trigger_a"},
        "awg_trigger_b": {"instrument": "core", "device": "awg_trigger_b"},
    },
    "sync_groups": {
        "qubit_awg": {
            "cards": ["A", "B"],
            # Channel aliases above; the compiler owns their trigger pulses.
            "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b"},
        },
    },
    "signals": {
        "q0": {
            "rf": "nuclear_rf",
            "clock": "clock",
            "rydberg": "rydberg",
        },
        "camera": "camera",
    },
}
