#!/usr/bin/env python3
"""Report current code size and recent Git churn without changing the repo."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]

LANGUAGES = {
    ".css": "CSS",
    ".html": "HTML",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ps1": "PowerShell",
    ".py": "Python",
    ".sh": "Shell",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".yaml": "YAML",
    ".yml": "YAML",
}

EXCLUDED_PREFIXES = (
    ".next/",
    ".openai/",
    "node_modules/",
    "public/data/",
)

COMMIT_PREFIX = "@@commit@@"


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_source_path(path: str) -> bool:
    normalized = normalize_path(path)
    if any(normalized.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    return Path(normalized).suffix.lower() in LANGUAGES


def run_git(*args: str, root: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def tracked_source_paths(root: Path = ROOT) -> list[str]:
    return sorted(
        path
        for path in run_git("ls-files", root=root).splitlines()
        if path and is_source_path(path)
    )


def file_metrics(root: Path, paths: Iterable[str]) -> list[dict]:
    rows: list[dict] = []
    for raw_path in paths:
        path = normalize_path(raw_path)
        absolute = root / Path(path)
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        rows.append(
            {
                "path": path,
                "language": LANGUAGES[absolute.suffix.lower()],
                "lines": len(lines),
                "nonblank_lines": sum(bool(line.strip()) for line in lines),
                "bytes": absolute.stat().st_size,
            }
        )
    return rows


def parse_numstat_log(raw: str) -> list[dict]:
    commits: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        if line.startswith(COMMIT_PREFIX):
            if current is not None:
                commits.append(current)
            parts = line[len(COMMIT_PREFIX) :].split("\t", 2)
            if len(parts) != 3:
                current = None
                continue
            current = {
                "sha": parts[0],
                "timestamp": parts[1],
                "subject": parts[2],
                "additions": 0,
                "deletions": 0,
                "files_changed": 0,
                "files": [],
            }
            continue
        if current is None or not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or not is_source_path(parts[2]):
            continue
        additions = 0 if parts[0] == "-" else int(parts[0])
        deletions = 0 if parts[1] == "-" else int(parts[1])
        current["additions"] += additions
        current["deletions"] += deletions
        current["files_changed"] += 1
        current["files"].append(
            {
                "path": normalize_path(parts[2]),
                "additions": additions,
                "deletions": deletions,
            }
        )
    if current is not None:
        commits.append(current)
    return commits


def recent_commit_metrics(limit: int, root: Path = ROOT) -> list[dict]:
    raw = run_git(
        "log",
        f"-n{limit}",
        "--date=iso-strict",
        f"--format={COMMIT_PREFIX}%H%x09%aI%x09%s",
        "--numstat",
        "--no-renames",
        "--",
        ".",
        root=root,
    )
    return parse_numstat_log(raw)


def build_report(root: Path = ROOT, history: int = 20, top: int = 15) -> dict:
    paths = tracked_source_paths(root)
    files = file_metrics(root, paths)
    commits = recent_commit_metrics(history, root)
    language_totals: dict[str, dict[str, int]] = {}
    for row in files:
        totals = language_totals.setdefault(
            row["language"],
            {"files": 0, "lines": 0, "nonblank_lines": 0},
        )
        totals["files"] += 1
        totals["lines"] += row["lines"]
        totals["nonblank_lines"] += row["nonblank_lines"]

    churn: Counter[str] = Counter()
    for commit in commits:
        for changed in commit["files"]:
            churn[changed["path"]] += changed["additions"] + changed["deletions"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": run_git("rev-parse", "HEAD", root=root).strip(),
        "summary": {
            "files": len(files),
            "lines": sum(row["lines"] for row in files),
            "nonblank_lines": sum(row["nonblank_lines"] for row in files),
            "recent_commits": len(commits),
            "recent_additions": sum(row["additions"] for row in commits),
            "recent_deletions": sum(row["deletions"] for row in commits),
        },
        "languages": dict(
            sorted(language_totals.items(), key=lambda item: item[1]["lines"], reverse=True)
        ),
        "largest_files": sorted(files, key=lambda row: row["lines"], reverse=True)[:top],
        "recent_hotspots": [
            {"path": path, "churn": count} for path, count in churn.most_common(top)
        ],
        "recent_commits": commits,
    }


def render_text(report: dict) -> str:
    summary = report["summary"]
    rows = [
        "DavosBot code metrics",
        f"Commit: {report['commit'][:12]}",
        (
            f"Tracked source: {summary['files']:,} files, "
            f"{summary['lines']:,} lines ({summary['nonblank_lines']:,} nonblank)"
        ),
        (
            f"Recent churn: {summary['recent_commits']} commits, "
            f"+{summary['recent_additions']:,}/-{summary['recent_deletions']:,}"
        ),
        "",
        "Languages:",
    ]
    for language, totals in report["languages"].items():
        rows.append(
            f"  {language:<12} {totals['lines']:>8,} lines  {totals['files']:>4} files"
        )

    rows.extend(["", "Largest files:"])
    for index, item in enumerate(report["largest_files"], 1):
        rows.append(f"  {index:>2}. {item['path']} ({item['lines']:,} lines)")

    rows.extend(["", "Recent churn hotspots:"])
    if report["recent_hotspots"]:
        for index, item in enumerate(report["recent_hotspots"], 1):
            rows.append(f"  {index:>2}. {item['path']} ({item['churn']:,} changed lines)")
    else:
        rows.append("  No source-file churn in the selected history window.")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        type=int,
        default=20,
        help="Recent commits to include in churn metrics (default: 20).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Largest files and hotspots to show (default: 15).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output file. By default the report is printed only.",
    )
    args = parser.parse_args()

    if args.history < 1 or args.top < 1:
        parser.error("--history and --top must both be positive")

    try:
        report = build_report(history=args.history, top=args.top)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"code metrics failed: {detail}", file=sys.stderr)
        return 1

    rendered = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_text(report)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote code metrics to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
