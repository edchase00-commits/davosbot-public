#!/usr/bin/env python3
"""Guard private DavosBot checkouts against repo-shape and data-leak regressions."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PRIVATE_EXACT = {
    ".env",
    "MEMORY.md",
    "SOUL.md",
    "gc_state.json",
    "davosbot.db",
}

PRIVATE_PREFIXES = (
    ".claude/",
    ".codex/",
    ".cursor/",
    ".windsurf/",
    "backups/",
    "generated/",
    "exports/private/",
    "logs/",
)

ROOT_RUNTIME_MODULES = {
    "alerts.py",
    "brain.py",
    "commands.py",
    "config.py",
    "db.py",
    "group_chat.py",
    "image_access.py",
    "imessage.py",
    "memory.py",
    "openai_images.py",
    "permissions.py",
    "personality.py",
    "soul.py",
    "tools.py",
    "ufc.py",
}


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def git_paths(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def staged_paths() -> list[str]:
    return git_paths("diff", "--cached", "--name-only", "--diff-filter=ACMR")


def tracked_paths() -> list[str]:
    return git_paths("ls-files")


def check_paths(paths: list[str]) -> list[str]:
    errors: list[str] = []
    for raw_path in paths:
        path = normalize_path(raw_path)
        lower = path.lower()
        name = Path(path).name

        if path in PRIVATE_EXACT:
            errors.append(f"{path}: private runtime file must not be tracked or staged")
        if any(lower.startswith(prefix) for prefix in PRIVATE_PREFIXES):
            errors.append(f"{path}: private/generated directory content must stay untracked")
        if lower.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log")):
            errors.append(f"{path}: generated/runtime artifact must stay untracked")
        if path.startswith("personalities/") and path != "personalities/example.md":
            errors.append(f"{path}: local persona files are private; only personalities/example.md is tracked")
        if "/" not in path and name in ROOT_RUNTIME_MODULES:
            errors.append(f"{path}: runtime modules belong under davosbot/; root is for wrappers/docs only")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true", help="Check staged paths for pre-commit use.")
    scope.add_argument("--all", action="store_true", help="Check all tracked paths.")
    parser.add_argument("paths", nargs="*", help="Explicit paths to check.")
    args = parser.parse_args()

    if args.paths:
        paths = args.paths
    elif args.staged:
        paths = staged_paths()
    else:
        paths = tracked_paths()

    errors = check_paths(paths)
    if not errors:
        return 0

    print("[davosbot guard] repository cleanliness check failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
