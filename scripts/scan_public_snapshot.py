#!/usr/bin/env python3
"""Scan a sanitized public DavosBot snapshot for private markers.

The scanner intentionally avoids external tools so CI, Windows, and local
release checks all use the same leak gate.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


OWNER_FIRST_NAME = "Et" + "han"
WINDOWS_USER = "ed" + "cha"
MAC_USER = "et" + "han" + "chase"

PRIVATE_EXACT_PATHS = {
    ".env",
    "MEMORY.md",
    "SOUL.md",
    "gc_state.json",
    "davosbot.db",
}

PRIVATE_PATH_PREFIXES = (
    ".claude/",
    ".codex/",
    ".cursor/",
    ".windsurf/",
    "backups/",
    "generated/",
    "exports/private/",
    "logs/",
)

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "venv",
    ".venv",
}

CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("owner_first_name", re.compile(rf"\b{re.escape(OWNER_FIRST_NAME)}\b", re.IGNORECASE)),
    ("windows_user", re.compile(re.escape(WINDOWS_USER), re.IGNORECASE)),
    ("mac_user", re.compile(re.escape(MAC_USER), re.IGNORECASE)),
    ("private_windows_path", re.compile(r"C:\\Users\\" + re.escape(WINDOWS_USER), re.IGNORECASE)),
    ("private_mac_path", re.compile(r"/Users/" + re.escape(MAC_USER), re.IGNORECASE)),
    ("github_token", re.compile(r"gh[pousr]_[0-9A-Za-z_]{20,}")),
    ("gemini_api_key", re.compile(("AI" + "za") + r"[0-9A-Za-z_-]{20,}")),
)


@dataclass(frozen=True)
class Finding:
    path: str
    label: str
    line: int | None = None

    def format(self) -> str:
        if self.line is None:
            return f"{self.path}: {self.label}"
        return f"{self.path}:{self.line}: {self.label}"


def normalize_path(path: Path) -> str:
    normalized = path.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def should_skip_file(relative_path: Path, include_tests: bool) -> bool:
    parts = set(relative_path.parts)
    if parts & SKIP_DIRS:
        return True
    return not include_tests and relative_path.parts[:1] == ("tests",)


def iter_files(root: Path, include_tests: bool = False):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if should_skip_file(relative_path, include_tests):
            continue
        yield path, normalize_path(relative_path)


def path_findings(relative_path: str) -> list[Finding]:
    lower = relative_path.lower()
    findings: list[Finding] = []
    if relative_path in PRIVATE_EXACT_PATHS:
        findings.append(Finding(relative_path, "private runtime file present"))
    if any(lower.startswith(prefix) for prefix in PRIVATE_PATH_PREFIXES):
        findings.append(Finding(relative_path, "private/generated path present"))
    if lower.endswith((".db", ".sqlite", ".sqlite3", ".log")):
        findings.append(Finding(relative_path, "runtime artifact present"))
    if relative_path.startswith("personalities/") and relative_path != "personalities/example.md":
        findings.append(Finding(relative_path, "private persona file present"))
    return findings


def content_findings(path: Path, relative_path: str) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return [Finding(relative_path, f"could not read file: {exc.__class__.__name__}")]

    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in CONTENT_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(relative_path, f"private marker: {label}", line_number))
    return findings


def scan_snapshot(root: Path, include_tests: bool = False) -> list[Finding]:
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    findings: list[Finding] = []
    for path, relative_path in iter_files(root, include_tests=include_tests):
        path_issues = path_findings(relative_path)
        findings.extend(path_issues)
        if path_issues:
            continue
        findings.extend(content_findings(path, relative_path))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Path to the exported public snapshot.")
    parser.add_argument("--include-tests", action="store_true", help="Also scan test fixtures.")
    args = parser.parse_args()

    try:
        findings = scan_snapshot(args.snapshot, include_tests=args.include_tests)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"[davosbot guard] invalid snapshot path: {exc}", file=sys.stderr)
        return 2

    if not findings:
        print("Public snapshot private-marker scan passed.")
        return 0

    print("[davosbot guard] public snapshot private-marker scan failed:", file=sys.stderr)
    for finding in findings:
        print(f"  - {finding.format()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
