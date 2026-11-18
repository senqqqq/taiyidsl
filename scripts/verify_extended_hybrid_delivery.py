"""Verify this extension's saved software evidence without accessing hardware.

Run after the regression and selected real ARTIQ compile jobs. This checks
existing source/ELF/file hashes and local HTML references; it does not certify
browser rendering, analog waveforms or physical execution.
"""

import hashlib
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from taiyidsl import CompilationStore

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/extended_hybrid"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(65536), b""):
            digest.update(part)
    return digest.hexdigest()


def check_compile(report_path, source):
    report = read(report_path)
    assert report["status"] == "passed" and report["target"] == "rv32g"
    assert not report["kernels_executed"] and not report["hardware_verified"]
    normalized = Path(source).read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(normalized).hexdigest() == report["source_sha256"]
    for pin in report.get("native_source_pins", []):
        assert sha(pin["path"]) == pin["sha256"]
    for kernel in report["kernels"]:
        assert kernel["status"] == "passed"
        assert sha(kernel["elf"]) == kernel["elf_sha256"]
        with Path(kernel["elf"]).open("rb") as stream:
            assert stream.read(4) == b"\x7fELF"
    return {
        "report": str(report_path),
        "source": str(source),
        "source_sha256": report["source_sha256"],
        "kernels_compiled": len(report["kernels"]),
    }


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        key = "href" if tag == "a" else "src" if tag == "script" else None
        if key:
            self.targets.extend(value for name, value in attrs if name == key)


def main():
    regression = OUTPUT / "final_regression.xml"
    cases = list(ET.parse(regression).iter("testcase"))
    assert cases and all(
        not any(item.tag in {"failure", "error", "skipped"} for item in case)
        for case in cases
    ), "Final regression must have no unresolved failures or skipped cases"
    records = [
        check_compile(
            ROOT / "output/native_components/artiq_compile/compile_report.json",
            ROOT / "output/native_components/export/artiq/experiment.py",
        )
    ]
    extra = ROOT / "output/native_components/additional"
    for name in ("union", "square", "ram", "polar"):
        folder = "union_initializer" if name == "union" else f"named_{name}"
        records.append(
            check_compile(
                extra / f"compile_{name}/compile_report.json",
                extra / folder / "experiment.py",
            )
        )
    rb = read(ROOT / "output/rb_artiq_compile/verification.json")
    for case in rb["cases"]:
        records.append(check_compile(case["report"], case["source"]))
    stream_root = ROOT / "output/artiq_spectrum/rb_2q_stream"
    store = CompilationStore.open(stream_root / "compiled_store")
    mock = read(stream_root / "mock_run/offline_summary.json")
    view = read(stream_root / "viewer/report.json")
    assert store.sha256 == mock["artifact_sha256"] == view["artifact_sha256"]
    assert mock["data_commits"] == mock["new_mock_dispatches"]
    assert mock["dispatches_on_completed_resume"] == 0
    assert not mock["hardware_executed"] and not mock["generated_kernels_executed"]
    pages = [
        ROOT / f"output/artiq_spectrum/{name}/index.html"
        for name in ("rb_1q", "rb_2q", "cards_4")
    ] + [
        stream_root / "viewer/index.html",
        OUTPUT / "capture/index.html",
        OUTPUT / "greenmot_ramsey/index.html",
    ]
    for page in pages:
        parser = Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for target in parser.targets:
            url = urlsplit(target)
            if not url.scheme and not url.netloc and url.path:
                assert (page.parent / unquote(url.path)).is_file(), (page, target)
    result = {
        "status": "software_checks_passed",
        "regression": {
            "cases": len(cases),
            "report": str(regression),
            "sha256": sha(regression),
        },
        "real_artiq_compile": records,
        "kernels_compiled": sum(item["kernels_compiled"] for item in records),
        "stream_artifact_sha256": store.sha256,
        "mock_run": mock,
        "html_pages_static_links_checked": [str(page) for page in pages],
        "browser_rendering_verified": False,
        "browser_blocker": (
            "Desktop approval timed out; browser tool disallows file:// URLs"
        ),
        "hardware_verified": False,
        "kernels_executed_on_hardware": False,
    }
    path = OUTPUT / "verification_summary.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"{len(cases)} tests; {result['kernels_compiled']} real-compiled kernels; "
        f"{len(pages)} static pages"
    )
    print(path)


if __name__ == "__main__":
    main()
