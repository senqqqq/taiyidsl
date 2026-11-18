"""Check stored regression, baseline and compile evidence without hardware IO."""

import hashlib
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from verify_section_plan_baseline import compare

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/section_phase_chunk_qop"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def results(path):
    return {
        (case.attrib["classname"], case.attrib["name"]): not any(
            child.tag in {"failure", "error", "skipped"} for child in case
        )
        for case in ET.parse(path).iter("testcase")
    }


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.targets.extend(value for name, value in attrs if name == "href")


def main():
    full = results(OUTPUT / "test-results-final.xml")
    followup = results(OUTPUT / "test-results-ttl-compat.xml")
    assert set(followup) <= set(full)
    combined = {**full, **followup}
    assert all(combined.values()), [
        key for key, passed in combined.items() if not passed
    ]
    before, after = [
        json.loads((OUTPUT / name).read_text(encoding="utf-8"))
        for name in ("before.json", "after.json")
    ]
    compare(before, after)
    artifacts = []
    checked_pages = 0
    for example, compilation in (
        ("section_chunk_rb", "artiq_section_chunk_rb"),
        ("greenmot_ramsey", "artiq_greenmot"),
    ):
        folder = ROOT / f"output/artiq_spectrum/{example}"
        report = json.loads((OUTPUT / compilation / "compile_report.json").read_text())
        assert report["status"] == "passed" and not report["kernels_executed"]
        # The ARTIQ export/compiler contract hashes UTF-8 with normalized LF.
        source = (folder / "export/artiq/experiment.py").read_text(encoding="utf-8")
        assert hashlib.sha256(source.encode()).hexdigest() == report["source_sha256"]
        for kernel in report["kernels"]:
            assert kernel["status"] == "passed"
            assert sha(Path(kernel["elf"])) == kernel["elf_sha256"]
        decoded = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        for source in decoded["execution"]["sources"].values():
            assert sha(folder / source["path"]) == source["sha256"]
        for page in [
            folder / "index.html",
            *sorted(folder.glob("chunk_*_page_*.html")),
        ]:
            parser = Links()
            parser.feed(page.read_text(encoding="utf-8"))
            for target in parser.targets:
                url = urlsplit(target)
                if not url.scheme and not url.netloc and url.path:
                    assert (page.parent / unquote(url.path)).is_file(), (page, target)
            checked_pages += 1
        artifacts.append(
            {
                "example": example,
                "source_sha256": report["source_sha256"],
                "kernels_compiled": len(report["kernels"]),
                "artiq_version": report["artiq_version"],
                "target": report["target"],
            }
        )
    summary = {
        "status": "passed_after_targeted_rechecks",
        "regression": {
            "full_report": "test-results-final.xml",
            "full_passed": sum(full.values()),
            "full_failed_before_final_fix": sum(not value for value in full.values()),
            "followup_report": "test-results-ttl-compat.xml",
            "followup_passed": sum(followup.values()),
            "unique_cases_verified": len(combined),
            "unresolved_failures": [],
            "note": (
                "Full run plus targeted verification of the final TTL "
                "compatibility fix; not a single clean full run."
            ),
        },
        "baseline_experiments": list(before),
        "baseline_sha256": sha(OUTPUT / "before.json"),
        "current_snapshot_sha256": sha(OUTPUT / "after.json"),
        "artiq_compile": artifacts,
        "report_pages_static_links_checked": checked_pages,
        "kernels_executed": False,
        "hardware_verified": False,
        "browser_rendering_verified": False,
        "toolchain_note": (
            "Ordinary compile helper logged CommKernelDummy.close cleanup "
            "AttributeError after successful compilation; source/ELF hashes "
            "independently checked."
        ),
    }
    target = OUTPUT / "verification_summary.json"
    target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"{len(combined)} cases covered; {len(before)} baselines; "
        f"{checked_pages} report pages checked."
    )
    print(target)


if __name__ == "__main__":
    main()
