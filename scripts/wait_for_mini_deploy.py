#!/usr/bin/env python3
"""Wait for the Mac Mini production checkout to deploy a pushed commit."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "macmini"
DEFAULT_REMOTE_ROOT = "/Users/<you>/projects/davosbot"


def _short_sha(value: str) -> str:
    return (value or "").strip()[:7]


def _run(args: list[str], cwd: Path = ROOT, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def current_local_sha() -> str:
    result = _run(["git", "rev-parse", "--short", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def remote_command(host: str, command: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return _run(["ssh", host, command], timeout=timeout)


def mini_head(host: str, remote_root: str) -> str:
    result = remote_command(
        host,
        f"git -C {shlex.quote(remote_root)} rev-parse --short HEAD",
        timeout=30,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def wait_for_head(host: str, remote_root: str, sha: str, timeout: int, interval: int) -> bool:
    wanted = _short_sha(sha)
    deadline = time.monotonic() + timeout
    last_seen = ""
    while time.monotonic() <= deadline:
        seen = mini_head(host, remote_root)
        if seen != last_seen:
            print(f"mini head: {seen or 'unknown'}")
            last_seen = seen
        if seen.startswith(wanted):
            return True
        time.sleep(interval)
    return False


def mini_pm2_statuses(host: str, remote_root: str) -> dict[str, str]:
    result = remote_command(
        host,
        f"cd {shlex.quote(remote_root)} && PATH=/opt/homebrew/bin:/usr/local/bin:$PATH pm2 jlist",
        timeout=30,
    )
    if result.returncode != 0:
        return {}
    try:
        processes = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {
        str(proc.get("name")): str(proc.get("pm2_env", {}).get("status", "unknown"))
        for proc in processes
        if proc.get("name")
    }


def wait_for_pm2_online(host: str, remote_root: str, timeout: int = 75, interval: int = 5) -> bool:
    wanted = {
        "davosbot",
        "davosbot-autodeploy",
        "davosbot-comfyui",
        "davosbot-local-image-worker",
    }
    deadline = time.monotonic() + timeout
    last_detail = ""
    while time.monotonic() <= deadline:
        statuses = mini_pm2_statuses(host, remote_root)
        detail = ", ".join(f"{name}={statuses.get(name, 'missing')}" for name in sorted(wanted))
        if detail != last_detail:
            print(f"pm2: {detail}")
            last_detail = detail
        if statuses and all(statuses.get(name) == "online" for name in wanted):
            return True
        time.sleep(interval)
    return False


def run_remote_step(host: str, remote_root: str, label: str, command: str, timeout: int = 120) -> bool:
    wrapped = f"cd {shlex.quote(remote_root)} && {command}"
    result = remote_command(host, wrapped, timeout=timeout)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if output:
        print(output)
    if result.returncode != 0:
        print(f"FAIL {label}: exit {result.returncode}")
        return False
    print(f"PASS {label}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", nargs="?", default="", help="SHA to wait for. Defaults to local HEAD.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH host alias.")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT, help="Production checkout path on the Mini.")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for auto-deploy.")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval in seconds.")
    parser.add_argument("--no-smoke", action="store_true", help="Skip runtime smoke after deploy.")
    parser.add_argument("--no-log-check", action="store_true", help="Skip fresh auto-deploy log check after deploy.")
    args = parser.parse_args()

    sha = _short_sha(args.sha or current_local_sha())
    if not sha:
        print("Could not determine target SHA.", file=sys.stderr)
        return 2

    print(f"Waiting for {args.host}:{args.remote_root} to reach {sha}")
    if not wait_for_head(args.host, args.remote_root, sha, args.timeout, args.interval):
        print(f"Timed out waiting for Mini deploy of {sha}.", file=sys.stderr)
        return 1

    ok = True
    ok = run_remote_step(
        args.host,
        args.remote_root,
        "mini_worktree_clean",
        'wt_status="$(git status --short)" && test -z "$wt_status"',
        timeout=30,
    ) and ok
    if not args.no_log_check:
        ok = run_remote_step(
            args.host,
            args.remote_root,
            "autodeploy_log",
            f"venv/bin/python scripts/check_autodeploy_log.py --sha {shlex.quote(sha)}",
            timeout=45,
        ) and ok
    if not args.no_smoke:
        pm2_ok = wait_for_pm2_online(args.host, args.remote_root)
        if not pm2_ok:
            print("FAIL pm2_wait: expected PM2 processes did not settle online")
            ok = False
        else:
            print("PASS pm2_wait")
        ok = run_remote_step(
            args.host,
            args.remote_root,
            "runtime_smoke",
            "venv/bin/python scripts/runtime_smoke.py",
            timeout=120,
        ) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
