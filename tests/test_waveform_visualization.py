"""RAM output intervals retain envelope details in the existing timing view."""

import json
from dataclasses import replace

from taiyidsl import Experiment, FlatTopGaussian, Qubit, play, reference_lab, us
from taiyidsl.visualization import _payload


def test_ram_output_has_shape_and_actual_samples_in_details():
    q = Qubit("q0")
    exp = Experiment("waveform_details", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, FlatTopGaussian(8 * us, 0.5 * us), amplitude=0.5)
    lab = reference_lab([q])
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(
        lab.setup.devices[name], nco_mode="legacy_tracking", ram_latency_mu=64
    )
    compiled = lab.compile(exp)
    outputs = [
        trace
        for trace in _payload(compiled)[0]["physical"]
        if trace["name"] == "dds_output"
    ]
    details = json.dumps(outputs)
    assert "flat_top_gaussian" in details
    assert "ram_asf" in details
    assert len(outputs) == 1
