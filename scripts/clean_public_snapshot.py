#!/usr/bin/env python3
"""Remove runtime artifacts created while validating a public snapshot."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


REMOVE_FILE_NAMES = {
    ".env",
    "MEMORY.md",
    "SOUL.md",
    "gc_state.json",
    "davosbot.db",
}

REMOVE_DIR_NAMES = {
    ".claude",
    ".codex",
    ".cursor",
    ".windsurf",
    "__pycache__",
    ".pytest_cache",
    "backups",
    "generated",
    "exports",
}

REMOVE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
}


def clean_snapshot(root: Path) -> list[str]:
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    removed: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True):
        relative_path = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_dir() and path.name in REMOVE_DIR_NAMES:
            shutil.rmtree(path)
            removed.append(relative_path + "/")
            continue
        if not path.is_file():
            continue
        if path.name in REMOVE_FILE_NAMES or path.suffix.lower() in REMOVE_SUFFIXES:
            path.unlink()
            removed.append(relative_path)
    return sorted(removed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Path to the exported public snapshot.")
    args = parser.parse_args()

    removed = clean_snapshot(args.snapshot)
    if removed:
        print(f"Removed {len(removed)} runtime artifact(s) from public snapshot.")
    else:
        print("No public snapshot runtime artifacts found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
