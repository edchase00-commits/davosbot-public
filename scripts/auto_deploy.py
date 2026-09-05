#!/usr/bin/env python3
"""Secure auto-deploy watcher for the Mac Mini runtime checkout.

This intentionally lives outside the bot process. It polls GitHub for the
configured branch, waits for CI to pass on the remote commit, validates the
candidate in a detached git worktree, then fast-forwards the live checkout and
restarts PM2.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# Pin the running watcher's isolation policy before loading any candidate code.
from scripts.quality_check import isolated_environment as _isolated_test_environment

DEFAULT_REPO = "example/davosbot"
DEFAULT_REQUIRED_WORKFLOW = "tests"
DEFAULT_RESTART_COMMAND = "PATH=/opt/homebrew/bin:/usr/local/bin:$PATH pm2 restart davosbot"
LOG = logging.getLogger("davosbot.auto_deploy")
_LAST_ALERT_AT: dict[str, float] = {}
_WAITING_SINCE: dict[str, float] = {}
_GH_FALLBACK_PATHS = (
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class CiStatus:
    ok: bool
    detail: str


@dataclass(frozen=True)
class DeployConfig:
    enabled: bool
    dry_run: bool
    repo_root: Path
    remote: str
    branch: str
    github_repo: str
    required_workflow: str
    require_ci: bool
    poll_seconds: int
    command_timeout_seconds: int
    preflight_command: str
    post_merge_command: str
    restart_command: str
    exit_after_deploy: bool
    worktree_root: Path
    status_path: Path


@dataclass(frozen=True)
class DeployOutcome:
    state: str
    detail: str
    local_sha: str = ""
    remote_sha: str = ""


Runner = Callable[..., CommandResult]


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _int_env(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _alert_cooldown_seconds() -> int:
    return max(0, _int_env("AUTO_DEPLOY_ALERT_COOLDOWN_SECONDS", "900"))


def _wait_alert_seconds() -> int:
    return max(0, _int_env("AUTO_DEPLOY_WAIT_ALERT_SECONDS", "0"))


def load_config() -> DeployConfig:
    """Load config from .env on every loop so the kill switch is live."""
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    worktree_root = Path(os.getenv("AUTO_DEPLOY_WORKTREE_ROOT", ".auto_deploy/worktrees"))
    status_path = Path(os.getenv("AUTO_DEPLOY_STATUS_PATH", ".auto_deploy/status.json"))
    if not worktree_root.is_absolute():
        worktree_root = PROJECT_ROOT / worktree_root
    if not status_path.is_absolute():
        status_path = PROJECT_ROOT / status_path

    return DeployConfig(
        enabled=_bool_env("AUTO_DEPLOY_ENABLED", "false"),
        dry_run=_bool_env("AUTO_DEPLOY_DRY_RUN", "false"),
        repo_root=PROJECT_ROOT,
        remote=os.getenv("AUTO_DEPLOY_REMOTE", "origin").strip() or "origin",
        branch=os.getenv("AUTO_DEPLOY_BRANCH", "master").strip() or "master",
        github_repo=os.getenv("AUTO_DEPLOY_GITHUB_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO,
        required_workflow=os.getenv("AUTO_DEPLOY_REQUIRED_WORKFLOW", DEFAULT_REQUIRED_WORKFLOW).strip(),
        require_ci=_bool_env("AUTO_DEPLOY_REQUIRE_CI", "true"),
        poll_seconds=max(30, _int_env("AUTO_DEPLOY_POLL_SECONDS", "120")),
        command_timeout_seconds=max(30, _int_env("AUTO_DEPLOY_COMMAND_TIMEOUT_SECONDS", "300")),
        preflight_command=os.getenv("AUTO_DEPLOY_PREFLIGHT_CMD", "bash scripts/validate.sh").strip(),
        post_merge_command=os.getenv(
            "AUTO_DEPLOY_POST_MERGE_CMD",
            "python3 -m py_compile main.py && python3 -m compileall -q davosbot",
        ).strip(),
        restart_command=os.getenv("AUTO_DEPLOY_RESTART_CMD", DEFAULT_RESTART_COMMAND).strip(),
        exit_after_deploy=_bool_env("AUTO_DEPLOY_EXIT_AFTER_DEPLOY", "true"),
        worktree_root=worktree_root,
        status_path=status_path,
    )


def _gh_executable() -> str:
    configured = os.getenv("AUTO_DEPLOY_GH_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("gh")
    if found:
        return found
    for candidate in _GH_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return "gh"


def run_command(
    command: list[str] | str,
    *,
    cwd: Path,
    timeout: int,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        if env is not None:
            return _run_isolated_command(command, cwd=cwd, timeout=timeout, shell=shell, env=env)
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            124,
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            f"command timed out after {timeout}s",
        )
    except OSError as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")


class PreflightCleanupError(RuntimeError):
    """A test child may remain alive; retain its isolated files for inspection."""


def _stop_isolated_processes(process, *, abnormal):
    failed = False
    try:
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP is not a Windows Job Object. Normal
            # Windows preflights must stay in the foreground; the Mini uses
            # the POSIX ownership/cleanup path on every terminal result.
            if abnormal:
                stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         check=False, timeout=10)
                failed = stopped.returncode != 0
        else:
            # A successful shell may have left a background child with closed
            # pipes. Stop its owned group even after the foreground exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except BaseException:
        failed = True
    # Even when tree termination fails, attempt to reap the direct child.
    try:
        if abnormal and process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    except BaseException:
        failed = True
    if failed:
        raise PreflightCleanupError("preflight process cleanup unverified; isolated files retained")


def _run_isolated_command(command, *, cwd, timeout, shell, env) -> CommandResult:
    """Stop the Mini preflight's owned group before removing isolated state."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    process = subprocess.Popen(command, cwd=str(cwd), shell=shell, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _stop_isolated_processes(process, abnormal=True)
        raise
    _stop_isolated_processes(process, abnormal=False)
    return CommandResult(process.returncode, stdout or "", stderr or "")


def _short(text: str, limit: int = 500) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit]


def _git(args: list[str], config: DeployConfig, runner: Runner = run_command) -> CommandResult:
    return runner(["git", *args], cwd=config.repo_root, timeout=config.command_timeout_seconds)


def _git_stdout(args: list[str], config: DeployConfig, runner: Runner = run_command) -> str:
    result = _git(args, config, runner)
    if result.returncode != 0:
        raise RuntimeError(_short(result.stderr or result.stdout or f"git {' '.join(args)} failed"))
    return result.stdout.strip()


def _remote_matches_repo(remote_url: str, github_repo: str) -> bool:
    repo = github_repo.strip().lower().removesuffix(".git")
    url = remote_url.strip().lower().removesuffix(".git")
    return (
        url == f"https://github.com/{repo}"
        or url == f"git@github.com:{repo}"
        or url.endswith(f"github.com/{repo}")
        or url.endswith(f"github.com:{repo}")
    )


def _ci_runs_status(runs: list[dict[str, Any]], *, sha: str, required_workflow: str = "") -> CiStatus:
    matching = [run for run in runs if (run.get("head_sha") or run.get("headSha")) == sha]
    if required_workflow:
        matching = [run for run in matching if str(run.get("name", "")).lower() == required_workflow.lower()]
    if not matching:
        workflow = f" workflow={required_workflow}" if required_workflow else ""
        return CiStatus(False, f"no GitHub Actions run found for {sha[:7]}{workflow}")

    pending = [run for run in matching if run.get("status") != "completed"]
    if pending:
        return CiStatus(False, f"CI still running for {sha[:7]}")

    failed = [run for run in matching if run.get("conclusion") not in {"success", "neutral", "skipped"}]
    if failed:
        details = ", ".join(f"{run.get('name', 'workflow')}={run.get('conclusion')}" for run in failed)
        return CiStatus(False, f"CI not green for {sha[:7]}: {details}")

    return CiStatus(True, f"CI green for {sha[:7]}")


def _ci_status_via_api(config: DeployConfig, sha: str) -> CiStatus:
    token = os.getenv("AUTO_DEPLOY_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        return CiStatus(False, "GitHub token not configured")

    url = f"https://api.github.com/repos/{config.github_repo}/actions/runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(
            url,
            params={"branch": config.branch, "head_sha": sha, "per_page": 20},
            headers=headers,
            timeout=15,
        )
        if response.status_code >= 400:
            return CiStatus(False, f"GitHub API returned HTTP {response.status_code}")
        runs = response.json().get("workflow_runs", [])
        return _ci_runs_status(runs, sha=sha, required_workflow=config.required_workflow)
    except requests.RequestException as exc:
        return CiStatus(False, f"GitHub API unavailable: {type(exc).__name__}")
    except (TypeError, ValueError) as exc:
        return CiStatus(False, f"GitHub API parse failed: {type(exc).__name__}")


def _ci_status_via_gh(config: DeployConfig, sha: str, runner: Runner = run_command) -> CiStatus:
    result = runner(
        [
            _gh_executable(),
            "run",
            "list",
            "--repo",
            config.github_repo,
            "--branch",
            config.branch,
            "--json",
            "headSha,status,conclusion,name,url",
            "-L",
            "20",
        ],
        cwd=config.repo_root,
        timeout=60,
    )
    if result.returncode != 0:
        return CiStatus(False, "GitHub CLI unavailable or unauthenticated")
    try:
        runs = json.loads(result.stdout or "[]")
    except ValueError:
        return CiStatus(False, "GitHub CLI returned invalid JSON")
    return _ci_runs_status(runs, sha=sha, required_workflow=config.required_workflow)


def check_ci_status(config: DeployConfig, sha: str, runner: Runner = run_command) -> CiStatus:
    if not config.require_ci:
        return CiStatus(True, "CI requirement disabled")

    api_status = _ci_status_via_api(config, sha)
    if api_status.ok or "token not configured" not in api_status.detail.lower():
        return api_status
    return _ci_status_via_gh(config, sha, runner)


def _safe_worktree_path(config: DeployConfig, sha: str) -> Path:
    root = config.worktree_root.resolve()
    path = (root / sha[:12]).resolve()
    if root not in path.parents:
        raise RuntimeError("unsafe worktree path")
    return path


def _remove_worktree(config: DeployConfig, path: Path, runner: Runner = run_command) -> None:
    if path.exists():
        runner(["git", "worktree", "remove", "--force", str(path)], cwd=config.repo_root, timeout=120)
    if path.exists():
        shutil.rmtree(path)


def _run_preflight(config: DeployConfig, sha: str, runner: Runner = run_command) -> CommandResult:
    worktree = _safe_worktree_path(config, sha)
    marker = worktree.with_name(worktree.name + ".cleanup-unverified.json")
    if marker.exists() or marker.is_symlink():
        raise RuntimeError("preflight cleanup is unverified for this SHA; operator review required")
    config.worktree_root.mkdir(parents=True, exist_ok=True)
    state = None
    cleanup_verified = True
    marker_created = False
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root.is_relative_to(config.repo_root.resolve()):
            raise RuntimeError("preflight test state must be outside the live checkout")
        state = Path(tempfile.mkdtemp(prefix="davosbot-preflight-", dir=temporary_root))
        env = _isolated_test_environment(state)
        # validate.sh must keep the watcher's dependency-equipped interpreter
        # after HOME and inherited PYTHON/DAVOSBOT_PROD_DIR are removed.
        env["PYTHON"] = sys.executable
        Path(env["SOUL_PATH"]).write_text("Synthetic DavosBot test identity.\n", encoding="utf-8")
        Path(env["MEMORY_PATH"]).write_text("", encoding="utf-8")
        # Claim the hold before launch so a crash or failed shutdown cannot be
        # retried by deleting files that a test descendant may still be using.
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        marker_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "target_sha": sha, "state_path": str(state.resolve())}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _remove_worktree(config, worktree, runner)
        add = runner(
            ["git", "worktree", "add", "--detach", str(worktree), sha],
            cwd=config.repo_root,
            timeout=config.command_timeout_seconds,
        )
        if add.returncode != 0:
            return add
        return runner(
            config.preflight_command,
            cwd=worktree,
            timeout=config.command_timeout_seconds,
            shell=True,
            env=env,
        )
    except PreflightCleanupError:
        cleanup_verified = False
        raise
    finally:
        if cleanup_verified:
            if state is not None:
                shutil.rmtree(state)
            if marker_created:
                _remove_worktree(config, worktree, runner)
                marker.unlink()


def _send_alert(event_type: str, message: str, metadata: dict[str, Any] | None = None) -> bool:
    key = f"{event_type}:{message}"
    now = time.monotonic()
    cooldown = _alert_cooldown_seconds()
    if cooldown and now - _LAST_ALERT_AT.get(key, 0) < cooldown:
        return False
    _LAST_ALERT_AT[key] = now
    try:
        from davosbot.alerts import send_owner_alert

        return send_owner_alert(event_type, message, metadata or {})
    except Exception as exc:  # pragma: no cover - alert failures must never stop deploy decisions
        LOG.warning("owner alert failed: %s", type(exc).__name__)
        return False


def _should_alert_waiting(remote_sha: str, detail: str) -> bool:
    """Return true only for explicitly configured prolonged CI waits."""
    threshold = _wait_alert_seconds()
    if threshold <= 0:
        return False
    key = f"{remote_sha}:{detail}"
    now = time.monotonic()
    first_seen = _WAITING_SINCE.setdefault(key, now)
    return now - first_seen >= threshold


def _write_status(config: DeployConfig, outcome: DeployOutcome) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "state": outcome.state,
        "detail": outcome.detail,
        "local_sha": outcome.local_sha,
        "remote_sha": outcome.remote_sha,
        "branch": config.branch,
        "remote": config.remote,
        "github_repo": config.github_repo,
        "enabled": config.enabled,
        "dry_run": config.dry_run,
    }
    _write_json_atomically(config.status_path, payload)


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _pending_path(config: DeployConfig) -> Path:
    return config.status_path.with_name(config.status_path.stem + "_pending.json")


def _pending_scope(config: DeployConfig) -> dict[str, str]:
    # Store a digest rather than a configurable command, which could contain
    # credentials. A changed preflight policy requires a fresh operator review.
    return {
        "repo_root": str(config.repo_root.resolve()),
        "remote": config.remote,
        "branch": config.branch,
        "github_repo": config.github_repo,
        "preflight": hashlib.sha256(config.preflight_command.encode("utf-8")).hexdigest(),
    }


def _read_pending(config: DeployConfig) -> dict[str, Any] | None:
    path = _pending_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("pending deploy checkpoint is unreadable; operator review required") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("phase") not in {"prepared", "post_merge", "restart"}
        or not all(
            isinstance(payload.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", payload[key])
            for key in ("previous_sha", "target_sha")
        )
        or payload.get("scope") != _pending_scope(config)
    ):
        raise RuntimeError("pending deploy checkpoint does not match this repo/preflight policy; operator review required")
    return payload


def _save_pending(config: DeployConfig, pending: dict[str, Any], phase: str) -> None:
    pending["phase"] = phase
    pending["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json_atomically(_pending_path(config), pending)


def deploy_once(
    config: DeployConfig,
    *,
    runner: Runner = run_command,
    ci_checker: Callable[[DeployConfig, str, Runner], CiStatus] = check_ci_status,
    alert: Callable[[str, str, dict[str, Any] | None], bool] = _send_alert,
) -> DeployOutcome:
    if not config.enabled:
        return DeployOutcome("disabled", "AUTO_DEPLOY_ENABLED is false")

    try:
        inside = _git_stdout(["rev-parse", "--is-inside-work-tree"], config, runner)
        if inside != "true":
            return DeployOutcome("blocked", "not inside a git work tree")

        branch = _git_stdout(["rev-parse", "--abbrev-ref", "HEAD"], config, runner)
        if branch != config.branch:
            detail = f"live branch is {branch}, expected {config.branch}"
            alert("auto_deploy_blocked", detail, {"branch": branch})
            return DeployOutcome("blocked", detail)

        remote_url = _git_stdout(["remote", "get-url", config.remote], config, runner)
        if not _remote_matches_repo(remote_url, config.github_repo):
            detail = "remote URL does not match configured GitHub repo"
            alert("auto_deploy_blocked", detail, {"remote": config.remote, "github_repo": config.github_repo})
            return DeployOutcome("blocked", detail)

        status = _git_stdout(["status", "--porcelain"], config, runner)
        if status:
            detail = "live checkout is dirty; refusing auto-deploy"
            alert("auto_deploy_blocked", detail, {"status_lines": len(status.splitlines())})
            return DeployOutcome("blocked", detail)

        fetch = _git(["fetch", "--prune", config.remote, config.branch], config, runner)
        if fetch.returncode != 0:
            detail = f"git fetch failed: {_short(fetch.stderr or fetch.stdout)}"
            alert("auto_deploy_failed", detail, None)
            return DeployOutcome("failed", detail)

        local_sha = _git_stdout(["rev-parse", "HEAD"], config, runner)
        remote_sha = _git_stdout(["rev-parse", f"{config.remote}/{config.branch}"], config, runner)
        pending = _read_pending(config)
        if pending:
            live_matches_target = local_sha == pending["target_sha"]
            prepared_on_previous = pending["phase"] == "prepared" and local_sha == pending["previous_sha"]
            if not live_matches_target and not prepared_on_previous:
                detail = "live HEAD differs from the pending deploy; refusing recovery on an unverified SHA"
                alert("auto_deploy_blocked", detail, {"local_sha": local_sha})
                return DeployOutcome("blocked", detail, local_sha, remote_sha)
            target_on_branch = _git(
                ["merge-base", "--is-ancestor", pending["target_sha"], remote_sha], config, runner,
            )
            if target_on_branch.returncode != 0:
                detail = "pending deploy SHA is no longer on the configured remote branch"
                alert("auto_deploy_blocked", detail, {"remote_sha": remote_sha})
                return DeployOutcome("blocked", detail, local_sha, remote_sha)
            # Complete the previously verified commit before considering a
            # newer branch head. CI below must still be green for this SHA.
            remote_sha = pending["target_sha"]
        elif local_sha == remote_sha:
            return DeployOutcome("up_to_date", "already on latest commit", local_sha, remote_sha)

        ancestor = _git(["merge-base", "--is-ancestor", "HEAD", f"{config.remote}/{config.branch}"], config, runner)
        if ancestor.returncode != 0:
            detail = "remote is not a fast-forward from live HEAD"
            alert("auto_deploy_blocked", detail, {"local_sha": local_sha, "remote_sha": remote_sha})
            return DeployOutcome("blocked", detail, local_sha, remote_sha)

        ci = ci_checker(config, remote_sha, runner)
        if not ci.ok:
            if _should_alert_waiting(remote_sha, ci.detail):
                alert("auto_deploy_waiting", ci.detail, {"remote_sha": remote_sha})
            return DeployOutcome("waiting", ci.detail, local_sha, remote_sha)

        if config.dry_run:
            detail = f"dry run: would deploy {remote_sha[:7]}"
            alert("auto_deploy_dry_run", detail, {"local_sha": local_sha, "remote_sha": remote_sha})
            return DeployOutcome("dry_run", detail, local_sha, remote_sha)

        if pending is None:
            preflight = _run_preflight(config, remote_sha, runner)
            if preflight.returncode != 0:
                detail = f"preflight failed: {_short(preflight.stderr or preflight.stdout)}"
                alert("auto_deploy_failed", detail, {"remote_sha": remote_sha})
                return DeployOutcome("failed", detail, local_sha, remote_sha)
            pending = {
                "version": 1,
                "scope": _pending_scope(config),
                "previous_sha": local_sha,
                "target_sha": remote_sha,
            }
            # Persist before fast-forwarding so even a watcher crash between
            # the merge and restart cannot be mistaken for an up-to-date bot.
            _save_pending(config, pending, "prepared")

        if local_sha != remote_sha:
            # Use the exact preflighted SHA, never a ref that another fetch
            # could advance between validation and this merge.
            merge = _git(["merge", "--ff-only", remote_sha], config, runner)
            if merge.returncode != 0:
                detail = f"fast-forward merge failed: {_short(merge.stderr or merge.stdout)}"
                alert("auto_deploy_failed", detail, {"remote_sha": remote_sha})
                return DeployOutcome("failed", detail, local_sha, remote_sha)

        live_sha = _git_stdout(["rev-parse", "HEAD"], config, runner)
        if live_sha != remote_sha:
            detail = "live HEAD changed before post-merge validation; pending deploy preserved"
            alert("auto_deploy_blocked", detail, {"local_sha": live_sha, "remote_sha": remote_sha})
            return DeployOutcome("blocked", detail, live_sha, remote_sha)
        _save_pending(config, pending, "post_merge")
        # Repeat the small live post-check before a restart retry. The full
        # preflight already passed for this exact commit; never repeat its FF.
        if config.post_merge_command:
            post = runner(
                config.post_merge_command,
                cwd=config.repo_root,
                timeout=config.command_timeout_seconds,
                shell=True,
            )
            if post.returncode != 0:
                detail = f"post-merge validation failed: {_short(post.stderr or post.stdout)}"
                alert("auto_deploy_failed", detail, {"remote_sha": remote_sha})
                return DeployOutcome("failed", detail, local_sha, remote_sha)

        _save_pending(config, pending, "restart")
        restart = runner(
            config.restart_command,
            cwd=config.repo_root,
            timeout=config.command_timeout_seconds,
            shell=True,
        )
        if restart.returncode != 0:
            detail = f"PM2 restart failed: {_short(restart.stderr or restart.stdout)}"
            alert("auto_deploy_failed", detail, {"remote_sha": remote_sha})
            return DeployOutcome("failed", detail, local_sha, remote_sha)

        _pending_path(config).unlink()
        detail = f"deployed {remote_sha[:7]}"
        previous_sha = pending["previous_sha"]
        alert("auto_deploy_success", detail, {"previous_sha": previous_sha, "deployed_sha": remote_sha})
        return DeployOutcome("deployed", detail, previous_sha, remote_sha)
    except (RuntimeError, OSError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else f"deploy checkpoint I/O failed: {type(exc).__name__}"
        alert("auto_deploy_failed", detail, None)
        return DeployOutcome("failed", detail)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("AUTO_DEPLOY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    once = "--once" in sys.argv
    while True:
        config = load_config()
        outcome = deploy_once(config)
        _write_status(config, outcome)
        LOG.info("%s: %s", outcome.state, outcome.detail)
        if outcome.state == "deployed" and config.exit_after_deploy and not once:
            LOG.info("exiting for PM2 supervisor restart after deploy")
            return 0
        if once:
            return 0 if outcome.state not in {"failed"} else 1
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
