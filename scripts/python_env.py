"""Resolve the best Python executable for DavosBot scripts."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Mapping


def _expand(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _candidate_prod_dir(root: Path, env: Mapping[str, str]) -> Path | None:
    configured = env.get("DAVOSBOT_PROD_DIR", "").strip()
    if configured:
        path = _expand(configured)
        if path.is_dir():
            return path

    home = env.get("HOME", "").strip()
    if home:
        default = _expand(Path(home) / "projects" / "davosbot")
        if default.is_dir():
            return default

    marker = f"{os.sep}.auto_deploy{os.sep}worktrees{os.sep}"
    root_text = str(root)
    if marker in root_text:
        base = Path(root_text.split(marker, 1)[0])
        if base.is_dir():
            return base
    return None


def _candidate_paths(root: Path, env: Mapping[str, str]) -> list[Path]:
    bases = [root]
    prod_dir = _candidate_prod_dir(root, env)
    if prod_dir and prod_dir not in bases:
        bases.append(prod_dir)

    candidates: list[Path] = []
    for base in bases:
        candidates.extend(
            [
                base / "venv" / "bin" / "python",
                base / ".venv" / "bin" / "python",
                base / "venv" / "Scripts" / "python.exe",
                base / ".venv" / "Scripts" / "python.exe",
            ]
        )
    return candidates


def resolve_python_bin(root: Path | None = None, env: Mapping[str, str] | None = None) -> str:
    """Prefer the repo or production venv before falling back to shell Python."""
    root = root or Path(__file__).resolve().parents[1]
    env = env or os.environ

    for candidate in _candidate_paths(root, env):
        if candidate.is_file():
            return str(candidate.resolve())

    env_python = (env.get("PYTHON") or "").strip()
    if env_python:
        found = shutil.which(env_python)
        if found:
            return str(Path(found).resolve())
        candidate = Path(env_python).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())

    if sys.executable:
        return sys.executable
    return shutil.which("python3") or shutil.which("python") or "python3"
