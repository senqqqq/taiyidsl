"""OFFLINE example: replace addresses and channels with the lab's device_db."""

device_db = {
    "core": {
        "type": "local",
        "module": "artiq.coredevice.core",
        "class": "Core",
        # Documentation-only address; substitute the laboratory core host.
        "arguments": {"host": "192.0.2.10", "ref_period": 1e-9, "target": "rv32g"},
    },
    "awg_trigger_a": {
        "type": "local",
        "module": "artiq.coredevice.ttl",
        "class": "TTLOut",
        "arguments": {"channel": 0},
    },
    "awg_trigger_b": {
        "type": "local",
        "module": "artiq.coredevice.ttl",
        "class": "TTLOut",
        "arguments": {"channel": 1},
    },
    "ttl_556_3DMOT_Sh": {
        "type": "local",
        "module": "artiq.coredevice.ttl",
        "class": "TTLOut",
        "arguments": {"channel": 2},
    },
    "ttl_Camera_Trig": {
        "type": "local",
        "module": "artiq.coredevice.ttl",
        "class": "TTLOut",
        "arguments": {"channel": 3},
    },
}
