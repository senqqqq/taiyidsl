"""Review the same two pulses at both scan points and under both shot orders."""

import math

import pytest

from taiyidsl.backends.artiq.verification import verify_equivalence


@pytest.mark.parametrize(
    "mode, order",
    [
        ("cyclic", [(0, 0), (1, 0), (0, 1), (1, 1)]),
        ("sequential", [(0, 0), (0, 1), (1, 0), (1, 1)]),
    ],
)
def test_scan_order_and_quantized_phase(mode, order):
    from examples.composed_phase_scan import compile_scan

    compiled = compile_scan(mode)
    assert list(compiled.execution_order()) == order
    for point, expected_start in zip(compiled.points, (4800, 6800), strict=True):
        plays = [e for e in point.logical_events if e.kind == "play"]
        assert [e.start_mu for e in plays] == [0, expected_start]
        assert plays[1].params["phase"] % (2 * math.pi) == pytest.approx(1.5 * math.pi)
        effects = [e for e in point.physical_events if e.kind == "dds_effect"]
        second = next(
            e for e in effects if e.start_mu == point.origin_mu + expected_start
        )
        # The reset takes effect at the boundary of the second pulse. Its
        # quarter-turn frame rotation is therefore visible directly in POW.
        assert second.params["square_pow"] == 0xC000
    assert verify_equivalence(compiled)["status"] == "passed"
