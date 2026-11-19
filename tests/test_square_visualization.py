"""The timing view displays actual effect boundaries, not buffered writes."""

from html import unescape

import pytest

from taiyidsl import Experiment, Qubit, Square, play, reference_lab, us
from taiyidsl.reporting.visualization import _payload


def test_continuous_square_is_segmented_at_effect_not_preparation():
    q = Qubit("q0")
    exp = Experiment("square_effect_view", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, Square(4 * us, phase=0, amplitude=0.25))
        play(q.raman, Square(4 * us, phase=1, amplitude=0.75))
    compiled = reference_lab([q]).compile(exp)
    point = compiled.points[0]
    outputs = [
        trace
        for trace in _payload(compiled)[0]["physical"]
        if trace["name"] == "dds_output"
    ]
    origin_us = point.origin_mu * 1e-3
    assert len(outputs) == 2
    assert outputs[0]["x"] == pytest.approx([origin_us, origin_us + 4])
    assert outputs[1]["x"] == pytest.approx([origin_us + 4, origin_us + 8])
    for trace, phase, amplitude in zip(outputs, (0.0, 1.0), (0.25, 0.75)):
        parameters = trace["meta"]["details"]["parameters"]
        assert parameters["phase"] == pytest.approx(phase)
        assert parameters["amplitude"] == pytest.approx(amplitude)
        hover = unescape(trace["hovertemplate"])
        assert f"<b>Phase (rad)</b>: {phase}" in hover
        assert f"<b>Amplitude</b>: {amplitude}" in hover
    prepares = [e for e in point.physical_events if e.kind == "dds_prepare"]
    assert prepares[1].start_mu < point.origin_mu + 4000
    assert outputs[0]["x"][1] == pytest.approx(outputs[1]["x"][0])
