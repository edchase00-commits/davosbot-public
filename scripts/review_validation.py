#!/usr/bin/env python3
"""Validate a pinned committed snapshot outside the live checkout with fake state."""

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_FILES = 20000
FULL_TEST_CODE = '''import pathlib, unittest
loader = unittest.TestLoader()
suite = unittest.TestSuite()
files = sorted(pathlib.Path("tests").glob("test*.py"))
if not files:
    raise RuntimeError("Full validation discovered no test files")
for path in files:
    loaded = loader.discover("tests", pattern=path.name)
    if loaded.countTestCases() == 0:
        raise RuntimeError("Test file is not discoverable by unittest: " + path.name)
    suite.addTests(loaded)
result = unittest.TextTestRunner().run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
PRIVATE_PARTS = {".env", "MEMORY.md", "SOUL.md", "gc_state.json", "davosbot.db",
                 "backups", "generated", ".git", ".work_bridge", ".auto_deploy"}


class ReviewError(RuntimeError):
    pass


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise subprocess.TimeoutExpired("review validation", 0)
    return seconds


@contextmanager
def single_run(path):
    """OS lock released on exit/crash; an existing file alone is not a lock."""
    if path.is_symlink():
        raise ReviewError("Review lock cannot be a symlink")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ReviewError("Another committed-snapshot validation is running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ReviewError("Another committed-snapshot validation is running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            # Closing the descriptor releases flock on POSIX.


def extract_snapshot(archive, destination):
    """Extract regular committed files only; never copy runtime/private state."""
    total = 0
    count = 0
    with tarfile.open(archive, "r:") as source:
        for member in source:
            count += 1
            parts = PurePosixPath(member.name).parts
            if (not parts or PurePosixPath(member.name).is_absolute()
                    or any(part in {"..", "."} or "\\" in part or ":" in part for part in parts)):
                raise ReviewError("Unsafe path in committed snapshot")
            if any(part in PRIVATE_PARTS for part in parts) or parts[:2] == ("exports", "private"):
                continue
            if not member.isfile() and not member.isdir():
                raise ReviewError("Committed snapshot contains a link or special file")
            total += member.size
            if total > MAX_ARCHIVE_BYTES or count > MAX_FILES:
                raise ReviewError("Committed snapshot exceeds review limits")
            target = destination.joinpath(*parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ReviewError("Snapshot path escaped review directory")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as reader, target.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)


def _run_phase(arguments, *, cwd, env, timeout):
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    process = subprocess.Popen(arguments, cwd=cwd, env=env, **options)
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == "nt":
            # Only the child PID created above is targeted, including its test
            # descendants. No shell command interpolation is involved.
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        raise
    return process.returncode


def run_review(root, *, timeout=650, temporary_root=None):
    # Only git reads run against production. Tests run from the exported commit,
    # never an auto-deploy worktree nested beneath production.
    root = root.resolve()
    temporary_root = Path(temporary_root or tempfile.gettempdir()).resolve()
    production_roots = [root, Path("/Users/<you>/projects/davosbot").resolve()]
    if os.environ.get("DAVOSBOT_PROD_DIR"):
        production_roots.append(Path(os.environ["DAVOSBOT_PROD_DIR"]).expanduser().resolve())
    if any(temporary_root.is_relative_to(path) for path in production_roots):
        raise ReviewError("Review temporary directory must be outside the production checkout")
    deadline = time.monotonic() + timeout
    key = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    with single_run(temporary_root / f"davosbot-review-{key}.lock"):
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, check=True, timeout=_remaining(deadline)).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ReviewError("Could not identify committed revision")
        print(f"Committed snapshot: {sha}", flush=True)
        with tempfile.TemporaryDirectory(prefix="davosbot-review-", dir=temporary_root) as temporary:
            base = Path(temporary)
            archive, review, state = base / "source.tar", base / "source", base / "state"
            review.mkdir()
            state.mkdir()
            with archive.open("wb") as output:
                subprocess.run(["git", "archive", "--format=tar", sha], cwd=root, stdout=output,
                               stderr=subprocess.PIPE, check=True, timeout=_remaining(deadline))
            if archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ReviewError("Committed archive exceeds review limits")
            extract_snapshot(archive, review)
            from scripts.quality_check import isolated_environment
            env = isolated_environment(state)
            env.update({"PYTHONPATH": str(review), "PYTHONPYCACHEPREFIX": str(state / "pycache")})
            Path(env["SOUL_PATH"]).write_text("Synthetic DavosBot test identity.\n", encoding="utf-8")
            Path(env["MEMORY_PATH"]).write_text("", encoding="utf-8")
            commands = (
                ("Full unittest suite", ["-c", FULL_TEST_CODE]),
                ("Compile", ["-m", "compileall", "-q", "main.py", "davosbot", "scripts", "tests"]),
                ("Guarded behavioral suites", ["scripts/quality_check.py", "all", "--timeout",
                                               str(max(1, int(_remaining(deadline))))]),
            )
            for label, arguments in commands:
                print(label + ": starting", flush=True)
                code = _run_phase([sys.executable, *arguments], cwd=review, env=env,
                                  timeout=_remaining(deadline))
                if code:
                    print(label + ": FAIL", flush=True)
                    return code if code > 0 else 1
                print(label + ": PASS", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=650)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        return run_review(ROOT, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print("Committed-snapshot validation timed out.", file=sys.stderr)
        return 124
    except (ReviewError, OSError, subprocess.CalledProcessError, tarfile.TarError):
        # Failure details from Git may include local configuration; no raw dump.
        print("Committed-snapshot validation could not complete; no passing result recorded.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
