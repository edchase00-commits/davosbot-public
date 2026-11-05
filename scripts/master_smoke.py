"""Run DavosBot's high-signal local smoke suite from the repo root.

This script is intentionally local/read-only. It does not commit, deploy,
restart PM2, clear logs, or touch production secrets.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class SmokeResult:
    name: str
    ok: bool
    detail: str


def _run(name: str, args: list[str], timeout: int = 180) -> SmokeResult:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    output = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode == 0:
        return SmokeResult(name, True, "ok")
    tail = "\n".join(output.splitlines()[-25:]) if output else f"exit {proc.returncode}"
    return SmokeResult(name, False, tail)


def _deterministic_checks() -> SmokeResult:
    try:
        from davosbot import commands, openai_images, personality
        from davosbot.text_safety import is_imessage_reaction, normalize_bot_text

        prompt = personality.build_system_prompt(user_text="api status and sports preferences")
        lowered = prompt.lower()
        required = [
            "never call owner or users `my g`",
            "unc tar heels",
            "fc barcelona",
            "indiana pacers",
            "seattle mariners",
            "do not invent live scores",
            "decatur behavior/style is not ambient",
        ]
        missing = [item for item in required if item not in lowered]
        sanitized = normalize_bot_text("my g 🔫 ✊🏿 💣 locked 😂 😭")
        if "my g" in sanitized.lower():
            missing.append("glitch phrase cleanup")
        if any(symbol not in sanitized for symbol in ("🔫", "💣", "✊")):
            missing.append("emoji preservation")
        if not is_imessage_reaction('Loved "ok"', 2000, "guid"):
            missing.append("reaction detection")
        ok, reason, _mime = openai_images.validate_image_path(str(ROOT / "missing-image.png"))
        if ok or "not found" not in reason:
            missing.append("image validation")
        with _PermissionPatch(commands):
            api_status = commands._cmd_api_status(commands.OWNER_ID or "owner")
        if "API/tool status" not in api_status or "ESPN sports" not in api_status:
            missing.append("api status")
        if missing:
            return SmokeResult("deterministic", False, "missing: " + ", ".join(missing))
        return SmokeResult("deterministic", True, "ok")
    except Exception as exc:
        return SmokeResult("deterministic", False, f"{type(exc).__name__}: {exc}")


class _PermissionPatch:
    def __init__(self, commands_module):
        self.commands = commands_module
        self.original = commands_module.check_action_permission

    def __enter__(self):
        self.commands.check_action_permission = lambda *_args, **_kwargs: None
        return self

    def __exit__(self, *_exc):
        self.commands.check_action_permission = self.original


def run_master_smoke(*, quick: bool = False) -> list[SmokeResult]:
    results = [
        _run("py_compile", [sys.executable, "-m", "py_compile", "main.py"]),
        _run("compileall_package", [sys.executable, "-m", "compileall", "-q", "davosbot"]),
        _deterministic_checks(),
    ]
    if not quick:
        results.append(_run("unit_tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests"], timeout=300))
    return results


def format_results(results: list[SmokeResult]) -> str:
    lines = []
    failures = []
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        lines.append(f"{status} {result.name}: {result.detail}")
        if not result.ok:
            failures.append(result.name)
    lines.append(f"Overall: {'PASS' if not failures else 'FAIL (' + ', '.join(failures) + ')'}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Skip full unittest discovery.")
    args = parser.parse_args()
    results = run_master_smoke(quick=args.quick)
    print(format_results(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
