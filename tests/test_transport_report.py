"""DDS reports expose command predictions without manufacturing DAC samples."""

import csv
import hashlib
import json
import runpy
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from taiyidsl.output_simulator import simulate_output

ROOT = Path(__file__).resolve().parents[1]


def test_three_card_report_records_commands_positions_and_live_hold(tmp_path):
    source = ROOT / "examples/artiq_spectrum/transport_dds.py"
    lab, experiment = runpy.run_path(str(source))["build_demo"]()
    compiled = lab.compile(experiment)
    result = compiled.export_report(tmp_path, source_path=source)
    compiled.assert_integrity()
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    point = report["points"][0]
    assert len(point["transport"]) == 2
    outbound, inbound = point["transport"]
    assert outbound["final_layout"] == inbound["initial_layout"]
    assert outbound["initial_layout"] == inbound["final_layout"]
    assert outbound["max_timing_quantization_error_s"] <= 3.2e-9
    card = point["groups"][0]["cards"]["C"]
    assert card["mode"] == "dds"
    assert card["terminal_state"] == "continuous_hold_until_session_close"
    assert "segments" not in card and "pulses" not in card
    folder = tmp_path / card["directory"]
    program = json.loads((folder / "dds_program.json").read_text(encoding="utf-8"))
    assert program["final_tones"] == program["initial_tones"]
    assert program["frames"][-1]["trigger_source"] == "card"
    assert all(t["amplitude"] > 0 for t in program["final_tones"])
    with (folder / "dds_trajectory.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert all(float(row["amplitude"]) > 0 for row in rows)
    assert float(rows[-1]["time_us"]) == program["frames"][-1]["at_mu"] * 0.0002
    assert {t["card"] for t in simulate_output(compiled)["traces"]} == {"A", "B"}
    assert not list(folder.glob("segment_*.bin"))
    for record in report["execution"]["sources"].values():
        assert (
            hashlib.sha256((tmp_path / record["path"]).read_bytes()).hexdigest()
            == record["sha256"]
        )

    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name in {"href", "src"} and value:
                    target = urlsplit(value)
                    if not target.scheme and target.path:
                        assert (tmp_path / unquote(target.path)).is_file(), value

    Links().feed(result["index"].read_text(encoding="utf-8"))
    assert (tmp_path / "point_0/transport_paths.svg").is_file()
    assert (folder / "frequency.svg").is_file()
    assert (folder / "amplitude.svg").is_file()
