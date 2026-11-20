#!/usr/bin/env python3
"""Print the DavosBot Codex handoff state without touching runtime data."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MINI_PROD_ROOT = Path("/Users/<you>/projects/davosbot")
MINI_WORK_ROOT = Path("/Users/<mac-user>/codex-work/davosbot")
WINDOWS_ROOT_SUFFIX = "users/<windows-user>/davosbot"


def _run(args: list[str], cwd: Path = ROOT, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _short_sha(value: str) -> str:
    return (value or "")[:7]


def workspace_kind(root: Path = ROOT) -> str:
    raw = root.as_posix().lower()
    normalized = root.resolve().as_posix().lower()
    candidates = {raw, normalized}
    if MINI_PROD_ROOT.as_posix().lower() in candidates:
        return "mini-production"
    if MINI_WORK_ROOT.as_posix().lower() in candidates:
        return "mini-codex-work"
    if normalized.replace("\\", "/").endswith(WINDOWS_ROOT_SUFFIX):
        return "windows-codex"
    return "unknown"


def git_info(root: Path = ROOT) -> dict[str, Any]:
    status = _run(["git", "status", "--short"], root)
    branch = _run(["git", "branch", "--show-current"], root)
    head = _run(["git", "log", "-1", "--oneline"], root)
    upstream = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
    remote = _run(["git", "rev-parse", "origin/master"], root)
    local = _run(["git", "rev-parse", "HEAD"], root)
    return {
        "status_ok": status.returncode == 0,
        "dirty_lines": [line for line in status.stdout.splitlines() if line.strip()],
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "upstream": upstream.stdout.strip() if upstream.returncode == 0 else "",
        "local_sha": local.stdout.strip() if local.returncode == 0 else "",
        "origin_master_sha": remote.stdout.strip() if remote.returncode == 0 else "",
    }


def auto_deploy_info() -> dict[str, Any]:
    path = MINI_PROD_ROOT / ".auto_deploy" / "status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "unreadable"}


def build_status(root: Path = ROOT) -> dict[str, Any]:
    git = git_info(root)
    return {
        "workspace": workspace_kind(root),
        "root": str(root),
        "platform": platform.system(),
        "git": git,
        "auto_deploy": auto_deploy_info(),
    }


def format_status(status: dict[str, Any], *, brief: bool = False) -> str:
    git = status["git"]
    auto = status.get("auto_deploy") or {}
    dirty = len(git["dirty_lines"])
    local = git.get("local_sha") or ""
    remote = git.get("origin_master_sha") or ""
    aligned = bool(local and remote and local == remote)
    lines = [
        "DavosBot Codex sync",
        f"- workspace: {status['workspace']}",
        f"- root: {status['root']}",
        f"- head: {git['head'] or 'unknown'}",
        f"- branch/upstream: {git['branch'] or 'unknown'} -> {git['upstream'] or 'none'}",
        f"- worktree: {'clean' if dirty == 0 else f'dirty ({dirty} file(s))'}",
        f"- origin/master: {_short_sha(remote) or 'unknown'}",
        f"- local matches origin/master: {'yes' if aligned else 'no'}",
    ]
    if auto:
        lines.append(
            "- auto-deploy: "
            f"{auto.get('state', 'unknown')} "
            f"local={_short_sha(auto.get('local_sha', '')) or 'unknown'} "
            f"remote={_short_sha(auto.get('remote_sha', '')) or 'unknown'}"
        )
    if brief:
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "Rules:",
            "- Windows Codex edits C:\\Users\\<windows-user>\\davosbot.",
            "- Mini phone Codex edits /Users/<mac-user>/codex-work/davosbot.",
            "- Mini production /Users/<you>/projects/davosbot is runtime/read-only except auto-deploy and smoke checks.",
            "- Read AGENTS.md, docs/RUNBOOK.md, and docs/TASKS.md before meaningful edits.",
            "",
            "Safe loop:",
            "1. Run this sync check at session start.",
            "2. If the worktree is clean, fast-forward from origin/master before editing.",
            "3. Validate locally, push, wait for GitHub Actions, let auto-deploy update production.",
            "4. Run Mini runtime smoke from production: venv/bin/python scripts/runtime_smoke.py.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--brief", action="store_true", help="Emit the compact status only.")
    args = parser.parse_args()
    status = build_status()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(format_status(status, brief=args.brief))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
