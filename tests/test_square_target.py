"""Target wiring validation also covers mixed timed and legacy DDS outputs."""

import pytest

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    Qubit,
    SignalMap,
    Square,
    compile_experiment,
    play,
    us,
)
from taiyidsl.artiq import generate_source, verify_export


def test_timed_square_cannot_alias_an_independent_legacy_update_line():
    q0, q1 = Qubit("q0"), Qubit("q1")
    setup = DeviceSetup(
        {
            "dds0": Device("dds0", "dds", square_latency_mu=80, update_group="a"),
            "dds1": Device("dds1", "dds", update_group="b"),
        }
    )
    exp = Experiment("mixed_wiring", [q0, q1])
    with exp.average(1), exp.realtime(), exp.parallel():
        play(q0.raman, Square(8 * us))
        play(q1.raman, Square(8 * us))
    compiled = compile_experiment(
        exp, setup, SignalMap({q0.raman: "dds0", q1.raman: "dds1"})
    )
    assert verify_export(compiled)["offline_export_check"] == "passed"
    source = generate_source(compiled).replace(
        "        self.result_adapter = None",
        "        self.result_adapter = None\n"
        "        self.device_1.io_update.target_o = self.device_0.io_update.target_o",
    )
    with pytest.raises(ValueError, match="IO_UPDATE wiring differs"):
        verify_export(compiled, source=source)
