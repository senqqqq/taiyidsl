from pathlib import Path

from test_artiq import make_compiled

from taiyidsl.compiler import compile_experiment
from taiyidsl.config.models import Qubit
from taiyidsl.experiments.templates import rabi
from taiyidsl.lab import reference_setup
from taiyidsl.reporting.visualization import visualize
from taiyidsl.units import us


def test_offline_html_has_both_views_and_selectors(tmp_path):
    compiled_fixture = make_compiled()
    path = visualize(compiled_fixture, tmp_path / "timeline.html")
    assert isinstance(path, Path) and path.is_absolute()
    html = path.read_text(encoding="utf-8")
    for text in (
        "Logical signals",
        "Physical resources",
        'id="shot"',
        "Loop structure",
        "Compilation diagnostics",
        "dds_config",
        "dds_output",
        "ref_time_mu",
        "Compiled model only. This view does not establish ARTIQ compilation or",
        "hardware timing acceptance.",
    ):
        assert text in html
    assert '<script src="https://' not in html
    assert "plotly.js" in html.lower()


def test_metadata_cannot_close_inline_script(tmp_path):
    compiled_fixture = make_compiled()
    compiled_fixture.points[0].coordinates["unsafe"] = (
        "</script><img src=x onerror=alert(1)>"
    )
    compiled_fixture.name = "<b>test</b>"
    html = visualize(compiled_fixture, tmp_path / "timeline.html").read_text(
        encoding="utf-8"
    )
    assert "</script><img" not in html
    assert "&lt;b&gt;test&lt;/b&gt;" in html


def test_real_compiled_snapshot_visualizes_without_ir_serialization_loss(tmp_path):
    q = Qubit("q0")
    setup, mapping = reference_setup([q])
    compiled = compile_experiment(rabi(q, [0 * us, 4 * us], shots=2), setup, mapping)
    path = compiled.visualize(tmp_path / "real.html")
    html = path.read_text(encoding="utf-8")
    assert compiled.provenance["content_hash"] in html
    assert "q0.raman" in html
    assert "urukul0_ch0" in html
    assert 'value="1"' in html
