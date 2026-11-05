"""Bound and serialize the Mini Codex cleanup process without editing runtime code."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 7200


def _process_start(pid: int) -> str | None:
    """Return a process identity, empty for dead, or None when unverifiable."""
    if pid <= 0 or os.name == "nt":
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    return proc.stdout.strip()


def _legacy_runner_active() -> bool | None:
    """A pre-supervisor lock has no PID; only reclaim after checking processes."""
    if os.name == "nt":
        return None
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode:
        return None
    processes = {}
    for line in proc.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        processes[int(fields[0])] = (int(fields[1]), fields[2])
    # Cron's shell command can contain this script's name too. Exclude this
    # launch's own ancestors so it does not mistake itself for an older run.
    ancestors = set()
    pid = os.getpid()
    while pid and pid not in ancestors:
        ancestors.add(pid)
        pid = processes.get(pid, (0, ""))[0]
    for pid, (_parent, command) in processes.items():
        if pid not in ancestors and "nightly_safe_cleanup_codex.sh" in command:
            return True
    return False


def cleanup_lock_state(project_root: Path) -> str:
    """Read-only status: idle, running, stale, or unknown (fail closed)."""
    lock_dir = project_root / ".auto_deploy" / "codex_cleanup.lock"
    if lock_dir.is_symlink():
        return "unknown"
    if not lock_dir.exists():
        return "idle"
    owner_path = lock_dir / "owner.json"
    if owner_path.is_symlink():
        return "unknown"
    if not owner_path.exists():
        active = _legacy_runner_active()
        return "unknown" if active is None else "running" if active else "stale"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        processes = owner["processes"]
        if not isinstance(processes, list) or not processes:
            return "unknown"
        unknown = False
        for entry in processes:
            pid, expected_start = int(entry["pid"]), entry["started"]
            if pid <= 0 or not isinstance(expected_start, str) or not expected_start:
                return "unknown"
            current_start = _process_start(pid)
            if current_start is None:
                unknown = True
            elif current_start == expected_start:
                return "running"
        return "unknown" if unknown else "stale"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _write_status(project_root: Path, state: str, *, exit_code: int | None = None) -> None:
    _write_json(project_root / ".auto_deploy" / "cleanup_status.json", {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
    })


def _stop_process_group(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        proc.terminate()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=15)
    # The shell can exit before a descendant that ignored SIGTERM. Terminate
    # remaining members of this run's own group before releasing its guard.
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_for_cleanup(proc: subprocess.Popen, timeout: float) -> tuple[int, str]:
    try:
        code = proc.wait(timeout=timeout)
        return code, "finished" if code == 0 else "failed"
    except subprocess.TimeoutExpired:
        _stop_process_group(proc)
        return 124, "timed_out"
    except BaseException:
        _stop_process_group(proc)
        raise


def supervise(project_root: Path, command: list[str], timeout: float) -> int:
    # flock belongs to the OS, so a reboot cannot leave a permanently held lock.
    # Retain the descriptor in the child too: a killed supervisor must not allow
    # a second cleanup to start while its existing child is still running.
    import fcntl

    state_dir = project_root / ".auto_deploy"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = state_dir / "codex_cleanup.lock"
    with (state_dir / "codex_cleanup.guard").open("a+") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Codex cleanup is already running.")
            return 75
        state = cleanup_lock_state(project_root)
        if state in {"running", "unknown"}:
            print("Existing cleanup lock is active or cannot be verified; leaving it intact.")
            return 75
        if state == "stale":
            # Delete only our known metadata, never recurse through a lock path.
            (lock_dir / "owner.json").unlink(missing_ok=True)
            lock_dir.rmdir()
        identity = _process_start(os.getpid())
        if not identity:
            print("Cannot verify cleanup supervisor identity; refusing to start.")
            return 1
        lock_dir.mkdir()
        owner = {"processes": [{"pid": os.getpid(), "started": identity}]}
        _write_json(lock_dir / "owner.json", owner)
        _write_status(project_root, "starting")
        proc = None
        try:
            env = dict(os.environ, DAVOSBOT_CLEANUP_SUPERVISED="1")
            proc = subprocess.Popen(
                command, cwd=project_root, env=env,
                start_new_session=True, pass_fds=(guard.fileno(),),
            )
            child_start = _process_start(proc.pid)
            if child_start:
                owner["processes"].append({"pid": proc.pid, "started": child_start})
                _write_json(lock_dir / "owner.json", owner)
            _write_status(project_root, "running")
            code, result = wait_for_cleanup(proc, timeout)
            _write_status(project_root, result, exit_code=code)
            return code
        except BaseException:
            if proc is not None:
                _stop_process_group(proc)
            _write_status(project_root, "failed", exit_code=1)
            raise
        finally:
            (lock_dir / "owner.json").unlink(missing_ok=True)
            lock_dir.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout < 60 or args.timeout > 14400:
        parser.error("provide a command and a timeout between 60 and 14400 seconds")
    if os.name == "nt":
        parser.error("cleanup supervision runs on the Mac Mini")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return supervise(args.project_root.resolve(), command, args.timeout)
    except KeyboardInterrupt:
        return 130
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Cleanup supervisor failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
