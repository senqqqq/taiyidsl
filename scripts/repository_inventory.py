"""Inventory local files and audit the source publication set; never stage/delete.

Git metadata is summarized separately. Directory links are not traversed. The
report directory itself is omitted to avoid recursively inventorying receipts.
Only source publication candidates are scanned for accidental secrets/addresses.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_REASONS = {
    ".venv": "uv-managed environment; reproduce from lockfile",
    ".toolchains": "separate ARTIQ compiler and vendor runtime",
    ".uv-cache": "download/build cache",
    "output": "local reports, data and verification evidence",
    "dist": "built distributions; publish separately if requested",
    "artiq_demo": "local supplied reference material",
    "local_config": "real laboratory configuration",
    "MEMORY.md": "local conversation and verification history",
    "section_phase_chunk_qop_plan.md": "historical plan; current decisions in docs",
    "taiyidsl混合控制方案.md": "historical plan; current decisions in docs",
    "randomized_benchmarking_helper.py": "unreferenced third-party source material",
    "dataset_db.mdb": "laboratory database, not a source publication input",
}
CACHE_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    "build",
    "htmlcov",
}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "github_fine_grained_token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "assigned_secret": re.compile(
        r"""(?i)(?:password|api_key|access_token|client_secret)\s*[:=]\s*["'][^"'\s]{12,}["']"""
    ),
}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def git_paths(*arguments):
    return set(filter(None, git(*arguments, "-z").decode("utf-8").split("\0")))


def classification(path, ignored):
    parts = Path(path).parts
    if not ignored:
        return (
            "publish",
            "source, documentation, example, test or project configuration",
        )
    if parts[0] in LOCAL_REASONS:
        return "local_only", LOCAL_REASONS[parts[0]]
    if any(p in CACHE_PARTS or p.startswith(".test-tmp") for p in parts):
        return "local_cache", "reproducible cache/temp; cleanup is optional"
    return "local_only", "matched Git ignore policy"


def audit_candidate(path):
    """Return locations only; never include a suspected secret value in a report."""
    findings = []
    if path.stat().st_size > 10 * 1024**2:
        findings.append({"kind": "large_source_file", "line": None})
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeError, OSError):
        return findings
    for number, line in enumerate(content.splitlines(), 1):
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                findings.append({"kind": kind, "line": number})
        for value in IPV4.findall(line):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETWORKS):
                findings.append({"kind": "review_device_address", "line": number})
                break
    if path.suffix == ".ipynb":
        notebook = json.loads(content)
        if any(
            c.get("outputs") or c.get("execution_count") is not None
            for c in notebook.get("cells", [])
            if c.get("cell_type") == "code"
        ):
            findings.append({"kind": "notebook_execution_output", "line": None})
    return findings


def inventory(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT / "output"):
        raise ValueError(
            "Inventory reports must stay inside the ignored output directory"
        )
    output.mkdir(parents=True, exist_ok=True)
    tracked = git_paths("ls-files", "--cached")
    candidates = tracked | git_paths("ls-files", "--others", "--exclude-standard")
    ignored = git_paths("ls-files", "--others", "--ignored", "--exclude-standard")
    tracked_ignored = git_paths(
        "ls-files", "--cached", "--ignored", "--exclude-standard"
    )
    modified = git_paths("diff", "--name-only")
    staged = git_paths("diff", "--cached", "--name-only")
    rows, findings = [], []
    for directory, directories, files in os.walk(ROOT, followlinks=False):
        parent = Path(directory)
        directories[:] = sorted(
            name
            for name in directories
            if name != ".git"
            and not (parent / name).is_symlink()
            and not (parent / name).is_junction()
            and (parent / name).resolve() != output
        )
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(ROOT).as_posix()
            if path.is_symlink():
                rows.append(
                    (relative, 0, "link", "review", "file link; not dereferenced")
                )
                continue
            size = path.stat().st_size
            # Git intentionally omits metadata such as nested .git files from
            # both candidate and ignored listings. Never promote those to source.
            excluded = (
                relative in ignored
                or relative in tracked_ignored
                or relative not in candidates
            )
            category, reason = classification(relative, excluded)
            status = (
                "tracked_ignored"
                if relative in tracked_ignored
                else "staged"
                if relative in staged
                else "modified"
                if relative in modified
                else "tracked"
                if relative in tracked
                else "ignored"
                if excluded
                else "untracked"
            )
            rows.append((relative, size, status, category, reason))
            if relative in candidates and not excluded:
                findings += [
                    dict(path=relative, **item) for item in audit_candidate(path)
                ]
    with (output / "file_inventory.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        writer = csv.writer(f)
        writer.writerow(("path", "bytes", "git_status", "classification", "reason"))
        writer.writerows(rows)
    published = sorted(r[0] for r in rows if r[3] == "publish")
    (output / "publish_files.txt").write_text(
        "\n".join(published) + "\n", encoding="utf-8"
    )
    source_hashes = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in published
    }
    (output / "publish_hashes.json").write_text(
        json.dumps(source_hashes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = Counter(r[3] for r in rows)
    summary = {
        "files": len(rows),
        "classification_counts": dict(counts),
        "classification_bytes": {
            kind: sum(r[1] for r in rows if r[3] == kind) for kind in counts
        },
        "omitted": [
            ".git/ (Git metadata and recovery refs)",
            output.relative_to(ROOT).as_posix(),
        ],
        "tracked_ignored": sorted(tracked_ignored),
        "findings": findings,
        "scan_scope": "heuristic source-only scan; not a security certification",
        "git_policy": "push selected branches only; never mirror recovery refs",
    }
    (output / "inventory_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"files": len(rows), "counts": dict(counts), "findings": len(findings)}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/release_prep")
    inventory(parser.parse_args().output)
