"""Safe local self-check loop for DavosBot.

Runs master smoke up to three times and writes a concise report. It does not
auto-edit code, commit, deploy, restart PM2, or clear the phone change log.
Codex should make targeted patches between runs, then rerun this script.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "exports" / "private"


def _run_master_smoke() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/master_smoke.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=360,
    )


def _write_report(attempts: list[subprocess.CompletedProcess[str]]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "self_fix_loop_report.md"
    lines = [
        "# DavosBot Self-Fix Loop Report",
        "",
        f"Attempts: {len(attempts)}",
        "Mode: local diagnostics only; no commits, deploys, PM2 restarts, or log clears.",
        "",
    ]
    for index, proc in enumerate(attempts, start=1):
        status = "PASS" if proc.returncode == 0 else "FAIL"
        lines.append(f"## Attempt {index}: {status}")
        body = (proc.stdout + "\n" + proc.stderr).strip()
        lines.append("```")
        lines.append(body[-6000:] if body else "(no output)")
        lines.append("```")
        lines.append("")
    if attempts and attempts[-1].returncode != 0:
        lines.extend(
            [
                "## Suggested next step",
                "Give Codex the failing section above, patch only the smallest relevant files, then rerun this script.",
                "Stop after three failed passes and escalate the remaining issue for manual review.",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--passes", type=int, default=3, help="Maximum smoke attempts, capped at 3.")
    args = parser.parse_args()
    max_passes = max(1, min(args.passes, 3))

    attempts: list[subprocess.CompletedProcess[str]] = []
    for _index in range(max_passes):
        proc = _run_master_smoke()
        attempts.append(proc)
        if proc.returncode == 0:
            break
        time.sleep(1)

    report = _write_report(attempts)
    print(f"Self-fix loop report: {report}")
    print((attempts[-1].stdout + "\n" + attempts[-1].stderr).strip())
    return attempts[-1].returncode


if __name__ == "__main__":
    raise SystemExit(main())
