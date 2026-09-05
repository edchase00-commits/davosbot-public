#!/usr/bin/env python3
"""Run focused behavioral regressions with synthetic state in a review checkout."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "intake": (
        "test_food_order.py", "test_food_checkout_permissions.py", "test_input_edge_cases.py", "test_owner_quality_intake.py",
        "test_log_priority_routing.py", "test_screenshot_log_intent.py", "test_group_mentions.py",
    ),
    "conversation": (
        "test_conversation_evaluator.py",
        "test_conversation_intent_routing.py", "test_conversation_corrections.py",
        "test_conversation_personality.py", "test_fast_chat_latency.py", "test_ollama_prompt_routing.py",
        "test_model_routing.py", "test_capability_gap_safety.py",
    ),
    "images": (
        "test_openai_image_routing.py", "test_openai_images.py", "test_image_access.py",
        "test_brain_image_logging.py", "test_image_conversation.py",
        "test_work_image_receipt_permissions.py",
        "test_work_image_input_permissions.py", "test_prepare_work_image.py",
    ),
    "schedules": (
        "test_cron_editing.py", "test_deterministic_reminders.py", "test_reminder_routing.py",
        "test_reminder_cancel_routing.py", "test_reminder_postcondition.py",
        "test_reminder_send_failures.py", "test_scheduler_retry.py",
        "test_scheduled_command_permissions.py", "test_cron_creation_permissions.py",
    ),
    "access": (
        "test_access_safety.py", "test_permissions_safety.py", "test_private_send.py",
        "test_normalize_handle.py", "test_tool_permission_matrix.py", "test_injection_patterns.py",
        "test_agentic_tool_permissions.py", "test_admin_permissions_durability.py",
        "test_checkout_browser_permissions.py",
        "test_scheduled_command_permissions.py", "test_cron_creation_permissions.py",
    ),
    "recovery": (
        "test_shared_state_permissions.py", "test_inbox_workers_permissions.py",
        "test_auto_deploy.py", "test_preflight_isolation_permissions.py", "test_ollama_recovery.py", "test_cleanup_status.py",
        "test_confirmed_cleanup_autorun.py", "test_session_heartbeat.py", "test_scheduler_retry.py",
        "test_cleanup_runner.py", "test_integrate_codex_branch.py",
        "test_work_cleanup_permissions.py", "test_work_cleanup_bridge.py", "test_work_bridge.py",
    ),
}

# No bot imports in this process. The child loads config only after environment
# isolation, then redirects package-root-derived state (GC state, backups, etc.).
_CHILD_CODE = r'''
import os, pathlib, sys, unittest
root, scratch = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
os.chdir(scratch)
blocked_events = set()

def block_live_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        blocked_events.add(event)
        raise RuntimeError("Quality checks require mocked network calls")
    if event == "subprocess.Popen":
        command = pathlib.Path(str(args[0])).name.lower()
        if command in {"osascript", "osascript.exe", "ssh", "ssh.exe", "pm2", "launchctl"}:
            blocked_events.add(event)
            raise RuntimeError("Quality checks cannot send messages or operate live services")
sys.addaudithook(block_live_io)

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
from davosbot import config
config.PROJECT_ROOT = scratch
from davosbot import imessage
def no_send(*args, **kwargs):
    blocked_events.add("imessage.send")
    raise RuntimeError("Quality checks require mocked message sends")
imessage.send_message = no_send
imessage.send_file = no_send

loader = unittest.TestLoader()
suite = unittest.TestSuite()
for filename in sys.argv[3:]:
    loaded = loader.discover(str(root / "tests"), pattern=filename)
    if loaded.countTestCases() == 0:
        raise RuntimeError("Selected test file contains no tests: " + filename)
    suite.addTests(loaded)
result = unittest.TextTestRunner(verbosity=1).run(suite)
if blocked_events:
    print("Unmocked I/O attempts blocked: " + ", ".join(sorted(blocked_events)), file=sys.stderr)
raise SystemExit(0 if result.wasSuccessful() and not blocked_events else 1)
'''


def check_review_checkout(root: Path) -> None:
    candidates = [Path("/Users/<you>/projects/davosbot"), Path.home() / "projects" / "davosbot"]
    if os.environ.get("DAVOSBOT_PROD_DIR"):
        candidates.append(Path(os.environ["DAVOSBOT_PROD_DIR"]).expanduser())
    for candidate in candidates:
        production = candidate.resolve()
        if root.resolve().is_relative_to(production) or Path.cwd().resolve().is_relative_to(production):
            raise ValueError("Run quality checks from a review checkout, never the production runtime checkout.")


def selected_files(names: list[str], root: Path = ROOT) -> list[str]:
    files = list(dict.fromkeys(filename for name in names for filename in SUITES[name]))
    for filename in files:
        if not (root / "tests" / filename).is_file():
            raise ValueError(f"Selected test file is missing: {filename}")
    if not files:
        raise ValueError("No tests selected.")
    return files


def isolated_environment(scratch: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
        "LANG", "LC_ALL", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    }}
    env.update({
        "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "DAVOSBOT_SUPPRESS_CONFIG_WARNINGS": "1",
        "HOME": str(scratch), "USERPROFILE": str(scratch), "TMPDIR": str(scratch),
        "TMP": str(scratch), "TEMP": str(scratch),
        "BOT_DB_PATH": str(scratch / "bot.sqlite"), "DB_PATH": str(scratch / "messages.sqlite"),
        "SOUL_PATH": str(scratch / "synthetic-soul.md"), "MEMORY_PATH": str(scratch / "synthetic-memory.md"),
        "GENERATED_DIR": str(scratch / "output"), "IMAGE_OUTPUT_DIR": str(scratch / "output" / "images"),
        "OPENAI_IMAGE_OUTPUT_DIR": str(scratch / "output" / "images"),
        "FANTASY_ACCESS_PRIVATE_KEY_PATH": str(scratch / "unconfigured-test-key.pem"),
        "OWNER_ID": "+15550000001", "MAC_MINI_APPLE_ID": "",
        "ADMIN_PASSWORD": "", "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "TAVILY_API_KEY": "",
        "SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_FROM_ADDRESS": "",
        "OWNER_ALERT_WEBHOOK_URL": "", "LOCAL_IMAGE_ENDPOINT": "",
    })
    return env


def run_tests(root: Path, files: list[str], timeout: int) -> int:
    check_review_checkout(root)
    if not files:
        raise ValueError("No tests selected.")
    for filename in files:
        if Path(filename).name != filename or not (root / "tests" / filename).is_file():
            raise ValueError(f"Selected test file is missing or invalid: {filename}")
    with tempfile.TemporaryDirectory(prefix="davosbot-quality-") as temporary:
        scratch = Path(temporary)
        env = isolated_environment(scratch)
        Path(env["SOUL_PATH"]).write_text("Synthetic DavosBot test identity.\n", encoding="utf-8")
        Path(env["MEMORY_PATH"]).write_text("", encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-c", _CHILD_CODE, str(root.resolve()), str(scratch), *files],
                cwd=scratch, env=env, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"Quality checks timed out after {timeout} seconds.", file=sys.stderr)
            return 124
        except OSError:
            print("Could not start the quality-check test process.", file=sys.stderr)
            return 2
    return result.returncode if result.returncode >= 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="*", choices=(*SUITES, "all"))
    parser.add_argument("--list", action="store_true", help="list focused suites without running tests")
    parser.add_argument("--timeout", type=int, default=300, help="maximum seconds for the complete selected run")
    args = parser.parse_args(argv)
    if args.list:
        for name, files in SUITES.items():
            print(f"{name}: {', '.join(files)}")
        return 0
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    names = list(SUITES) if not args.suites or "all" in args.suites else args.suites
    try:
        files = selected_files(names)
        check_review_checkout(ROOT)
        print(f"Running {', '.join(names)} ({len(files)} test files) with synthetic state.", flush=True)
        return run_tests(ROOT, files, args.timeout)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
