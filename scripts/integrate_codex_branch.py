#!/usr/bin/env python3
"""Merge green codex/* branches into master from GitHub Actions.

This is intentionally conservative. It only runs on pushed codex/* branches
after the tests workflow is green, skips red-tier/runtime-sensitive diffs, uses
a temporary worktree for the merge, validates the merged tree, and then pushes
master.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]

SAFE_CODEX_BRANCH_RE = re.compile(r"^codex/[A-Za-z0-9][A-Za-z0-9._/-]*$")

RED_TIER_EXACT = {
    ".env",
    "MEMORY.md",
    "SOUL.md",
    "gc_state.json",
    "davosbot.db",
    "ecosystem.config.js",
    ".github/workflows/integrate-codex-branch.yml",
    "scripts/auto_deploy.py",
    "scripts/integrate_codex_branch.py",
    "scripts/nightly_safe_cleanup_codex.sh",
    "davosbot/cleanup_runner.py",
}

RED_TIER_PREFIXES = (
    ".github/workflows/",
    "backups/",
    "generated/",
    "exports/private/",
    "logs/",
)

RED_TIER_RUNTIME_FILES = {
    "davosbot/db.py",
    "davosbot/memory.py",
    "davosbot/permissions.py",
    "davosbot/soul.py",
    "davosbot/tools.py",
}

RED_TIER_KEYWORDS = (
    "admin_password",
    "private-send",
    "private_send",
    "permission",
    "permissions",
    "secret",
    "secrets",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_safe_codex_branch(branch: str) -> bool:
    if not SAFE_CODEX_BRANCH_RE.match(branch):
        return False
    if any(token in branch for token in ("..", "@{", "\\", ":")):
        return False
    if branch.endswith(("/", ".lock")) or "//" in branch:
        return False
    return True


def red_tier_reason(path: str) -> str:
    normalized = normalize_path(path)
    lower = normalized.lower()
    name = Path(normalized).name.lower()

    if normalized in RED_TIER_EXACT:
        return "protected runtime or automation file"
    if any(lower.startswith(prefix) for prefix in RED_TIER_PREFIXES):
        return "protected runtime, generated, log, or workflow directory"
    if normalized in RED_TIER_RUNTIME_FILES:
        return "runtime-sensitive DavosBot module"
    if lower.endswith((".db", ".sqlite", ".sqlite3", ".log")):
        return "runtime artifact"
    if any(keyword in lower or keyword in name for keyword in RED_TIER_KEYWORDS):
        return "red-tier keyword"
    return ""


def blocked_paths(paths: list[str]) -> list[tuple[str, str]]:
    blocked: list[tuple[str, str]] = []
    for path in paths:
        reason = red_tier_reason(path)
        if reason:
            blocked.append((normalize_path(path), reason))
    return blocked


def notice(message: str) -> None:
    print(f"::notice title=Codex Integrator::{message}")


def warning(message: str) -> None:
    print(f"::warning title=Codex Integrator::{message}")


def run(command: list[str] | str, *, cwd: Path, shell: bool = False, check: bool = False) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        shell=shell,
        capture_output=True,
        text=True,
    )
    result = CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")
    if check and result.returncode != 0:
        rendered = command if isinstance(command, str) else " ".join(command)
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{rendered} failed: {detail[:500]}")
    return result


def git(args: list[str], *, cwd: Path = ROOT, check: bool = True) -> CommandResult:
    return run(["git", *args], cwd=cwd, check=check)


def git_stdout(args: list[str], *, cwd: Path = ROOT) -> str:
    return git(args, cwd=cwd, check=True).stdout.strip()


def changed_paths(base_ref: str, branch_ref: str) -> list[str]:
    output = git_stdout(["diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}..{branch_ref}"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def ensure_clean_checkout() -> None:
    status = git_stdout(["status", "--porcelain"])
    if status:
        raise RuntimeError("checkout is dirty; refusing integration")


def fetch_refs(remote: str, base: str, branch: str) -> None:
    git(
        [
            "fetch",
            "--prune",
            remote,
            f"+refs/heads/{base}:refs/remotes/{remote}/{base}",
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ]
    )


def prepare_worktree(worktree: Path, base_ref: str) -> None:
    if worktree.exists():
        git(["worktree", "remove", "--force", str(worktree)], check=False)
    if worktree.exists():
        shutil.rmtree(worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", str(worktree), base_ref])


def remove_worktree(worktree: Path) -> None:
    if worktree.exists():
        git(["worktree", "remove", "--force", str(worktree)], check=False)
    if worktree.exists():
        shutil.rmtree(worktree)


def configure_commit_author(worktree: Path) -> None:
    git(["config", "user.name", os.getenv("CODEX_INTEGRATOR_GIT_NAME", "davosbot-integrator[bot]")], cwd=worktree)
    git(
        [
            "config",
            "user.email",
            os.getenv("CODEX_INTEGRATOR_GIT_EMAIL", "davosbot-integrator[bot]@users.noreply.github.com"),
        ],
        cwd=worktree,
    )


def github_ref_sha(ref: str, *, token: str, repo: str) -> str:
    """Return GitHub's current SHA for a branch ref."""
    api_request = request.Request(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{ref}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with request.urlopen(api_request, timeout=20) as response:
            payload = json.load(response)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub ref lookup failed: HTTP {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"GitHub ref lookup failed: {exc.reason}") from exc

    sha = str(payload.get("object", {}).get("sha", "")).strip()
    if not sha:
        raise RuntimeError(f"GitHub ref lookup returned no SHA for {ref}")
    return sha


def wait_for_github_ref(
    ref: str,
    expected_sha: str,
    *,
    token: str,
    repo: str,
    attempts: int = 12,
    delay_seconds: float = 1.0,
) -> None:
    """Wait until GitHub's API resolves the branch to the pushed commit."""
    last_sha = ""
    for attempt in range(attempts):
        last_sha = github_ref_sha(ref, token=token, repo=repo)
        if last_sha == expected_sha:
            return
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise RuntimeError(
        f"GitHub ref {ref} did not reach {expected_sha[:7]} before dispatch "
        f"(last seen {last_sha[:7] or 'unknown'})"
    )


def dispatch_workflow(ref: str, workflow: str, *, expected_sha: str = "") -> None:
    """Start the master tests workflow after a GitHub-token push."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        message = "GITHUB_TOKEN and GITHUB_REPOSITORY are required to dispatch master tests"
        if os.getenv("GITHUB_ACTIONS") == "true":
            raise RuntimeError(message)
        warning(message)
        return

    if expected_sha:
        wait_for_github_ref(ref, expected_sha, token=token, repo=repo)

    payload = json.dumps({"ref": ref}).encode("utf-8")
    api_request = request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with request.urlopen(api_request, timeout=20) as response:
            if response.status != 204:
                raise RuntimeError(f"workflow dispatch returned HTTP {response.status}")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"workflow dispatch failed: HTTP {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"workflow dispatch failed: {exc.reason}") from exc

    notice(f"Dispatched {workflow} on {ref}")


def integrate(args: argparse.Namespace) -> int:
    branch = args.branch.strip()
    if not is_safe_codex_branch(branch):
        warning(f"Skipping non-codex or unsafe branch: {branch}")
        return 0

    ensure_clean_checkout()
    fetch_refs(args.remote, args.base, branch)

    base_ref = f"{args.remote}/{args.base}"
    branch_ref = f"{args.remote}/{branch}"
    base_sha = git_stdout(["rev-parse", base_ref])
    branch_sha = git_stdout(["rev-parse", branch_ref])

    if args.branch_sha and args.branch_sha != branch_sha:
        warning(
            f"Skipping {branch}: tested SHA {args.branch_sha[:7]} is not current branch head {branch_sha[:7]}"
        )
        return 0

    already_merged = git(["merge-base", "--is-ancestor", branch_sha, base_sha], check=False)
    if already_merged.returncode == 0:
        notice(f"{branch} is already included in {args.base}")
        return 0

    merge_base = git_stdout(["merge-base", base_ref, branch_ref])
    paths = changed_paths(merge_base, branch_ref)
    if not paths:
        notice(f"{branch} has no changed files relative to {args.base}")
        return 0

    blocked = blocked_paths(paths)
    if blocked:
        rendered = ", ".join(f"{path} ({reason})" for path, reason in blocked[:8])
        warning(f"Skipping {branch}: red-tier paths require human review: {rendered}")
        return 0

    worktree = (ROOT / args.worktree_root / branch.replace("/", "__")).resolve()
    root = ROOT.resolve()
    if root not in worktree.parents:
        raise RuntimeError("unsafe integration worktree path")

    prepare_worktree(worktree, base_ref)
    try:
        configure_commit_author(worktree)
        merge = git(["merge", "--no-edit", branch_sha], cwd=worktree, check=False)
        if merge.returncode != 0:
            warning(f"Skipping {branch}: merge conflict or merge failure")
            return 0

        validate = run(args.validate_command, cwd=worktree, shell=True)
        if validate.returncode != 0:
            sys.stdout.write(validate.stdout)
            sys.stderr.write(validate.stderr)
            raise RuntimeError(f"merged tree validation failed for {branch}")

        head = git_stdout(["rev-parse", "HEAD"], cwd=worktree)
        push = git(["push", args.remote, f"{head}:refs/heads/{args.base}"], cwd=worktree, check=False)
        if push.returncode != 0:
            sys.stdout.write(push.stdout)
            sys.stderr.write(push.stderr)
            raise RuntimeError(f"push to {args.base} failed for {branch}")

        if not args.skip_dispatch:
            dispatch_workflow(args.base, args.dispatch_workflow, expected_sha=head)

        notice(f"Merged {branch} into {args.base} at {head[:7]}")
        return 0
    finally:
        remove_worktree(worktree)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default=os.getenv("CODEX_INTEGRATION_BRANCH", os.getenv("GITHUB_REF_NAME", "")))
    parser.add_argument("--branch-sha", default=os.getenv("CODEX_INTEGRATION_BRANCH_SHA", ""))
    parser.add_argument("--base", default=os.getenv("CODEX_INTEGRATION_BASE", "master"))
    parser.add_argument("--remote", default=os.getenv("CODEX_INTEGRATION_REMOTE", "origin"))
    parser.add_argument("--worktree-root", default=os.getenv("CODEX_INTEGRATION_WORKTREE_ROOT", ".codex_integration"))
    parser.add_argument(
        "--validate-command",
        default=os.getenv("CODEX_INTEGRATION_VALIDATE_CMD", "bash scripts/validate.sh"),
    )
    parser.add_argument(
        "--dispatch-workflow",
        default=os.getenv("CODEX_INTEGRATION_DISPATCH_WORKFLOW", "tests.yml"),
    )
    parser.add_argument("--skip-dispatch", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return integrate(parse_args(argv or sys.argv[1:]))
    except RuntimeError as exc:
        print(f"::error title=Codex Integrator::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
