"""Static HTML/JS checks; these do not launch a browser or claim rendered QA."""

import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from taiyidsl import Qubit, compile_experiment, rabi, reference_setup, us


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.inside = False
        self.external = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside = True
            self.scripts.append("")
            if "src" in dict(attrs):
                self.external.append(dict(attrs)["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.scripts[-1] += data


def test_standalone_html_inline_scripts_parse(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable for static JavaScript syntax checking")
    q = Qubit("q0")
    exp = rabi(q, [0 * us, 4 * us], shots=2)
    exp.name = "test </script><script>alert('unexpected')</script>"
    compiled = compile_experiment(exp, *reference_setup([q]))
    path = compiled.visualize(tmp_path / "timeline.html")
    parser = Scripts()
    parser.feed(path.read_text(encoding="utf-8"))
    assert not parser.external
    assert len(parser.scripts) >= 3
    assert all("alert('unexpected')" not in s for s in parser.scripts)
    for i, script in enumerate(parser.scripts):
        target = tmp_path / f"script_{i}.js"
        target.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(target)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
