#!/usr/bin/env bash
set -euo pipefail

HOME_DIR="${HOME:-/Users/<mac-user>}"
PROD_DIR="${DAVOSBOT_PROD_DIR:-$HOME_DIR/projects/davosbot}"
WORK_DIR="${DAVOSBOT_CODEX_WORK_DIR:-$HOME_DIR/codex-work/davosbot}"
LOG_DIR="$PROD_DIR/.auto_deploy/codex_cleanup_logs"
MODE="nightly"
NOTIFY_OWNER=0

for arg in "$@"; do
  case "$arg" in
    --confirmed)
      MODE="confirmed"
      ;;
    --nightly)
      MODE="nightly"
      ;;
    --notify)
      NOTIFY_OWNER=1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ "${DAVOSBOT_CLEANUP_SUPERVISED:-}" != "1" ]]; then
  SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  exec "$PROD_DIR/venv/bin/python" "$PROD_DIR/davosbot/cleanup_runner.py" \
    --project-root "$PROD_DIR" \
    --timeout "${CODEX_CLEANUP_TIMEOUT_SECONDS:-7200}" \
    -- /bin/bash "$SCRIPT_PATH" "$@"
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${MODE}_safe_cleanup_$(date +%Y%m%d_%H%M%S).log"

notify_owner() {
  local status="$1"
  [[ "$NOTIFY_OWNER" != "1" ]] && return 0
  (
    cd "$PROD_DIR"
    "$PROD_DIR/venv/bin/python" - "$MODE" "$status" "$LOG_FILE" <<'PY'
import sys
from pathlib import Path

from davosbot import commands
from davosbot.config import OWNER_ID, PROJECT_ROOT
from davosbot.imessage import send_message

mode = sys.argv[1]
status = int(sys.argv[2])
log_file = Path(sys.argv[3])
rows = commands._fetch_change_log_rows()
buckets = commands._bucket_change_log_rows(rows)
counts = {color: len(items) for color, items in buckets.items()}
state = "finished" if status == 0 else "stopped"
try:
    rel_log = log_file.relative_to(Path(PROJECT_ROOT))
except ValueError:
    rel_log = log_file.name

message = (
    f"Codex safe cleanup {state} ({mode}). "
    f"Log rows now: GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}."
)
if rows:
    message += "\nText `log board` for what is still open."
else:
    message += "\nChange log is clear."
message += f"\nRun log: {rel_log}"

if OWNER_ID:
    send_message(OWNER_ID, message)
PY
  )
}

cleanup() {
  local status="$?"
  notify_owner "$status" || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

exec >>"$LOG_FILE" 2>&1

echo "== Davos $MODE safe cleanup =="
date

if [[ ! -e "$WORK_DIR/.git" ]]; then
  echo "Missing Codex workspace: $WORK_DIR"
  exit 1
fi

CODEX_BIN="${CODEX_BIN:-}"
if [[ -z "$CODEX_BIN" ]]; then
  CODEX_BIN="$(find "$HOME_DIR/.codex/packages/standalone/releases" -path "*/bin/codex" -type f 2>/dev/null | sort -V | tail -n 1 || true)"
fi
if [[ -z "$CODEX_BIN" || ! -x "$CODEX_BIN" ]]; then
  echo "Codex binary not found. Set CODEX_BIN or install Codex CLI."
  exit 1
fi

cd "$WORK_DIR"
git fetch origin master
RUN_ID="$(date +%Y%m%d_%H%M%S)_$$"
RUN_BRANCH="codex/cleanup-$RUN_ID"
RUN_DIR="$HOME_DIR/codex-work/davosbot-cleanup-runs/$RUN_ID"
mkdir -p "$(dirname "$RUN_DIR")"
git worktree add -b "$RUN_BRANCH" "$RUN_DIR" origin/master
cd "$RUN_DIR"
export DAVOSBOT_CLEANUP_RUN_BRANCH="$RUN_BRANCH"
export DAVOSBOT_PROD_DIR="$PROD_DIR"
echo "Cleanup branch: $RUN_BRANCH"
echo "Cleanup worktree: $RUN_DIR"

"$CODEX_BIN" \
  --cd "$RUN_DIR" \
  --sandbox danger-full-access \
  --ask-for-approval never \
  --model "${CODEX_CLEANUP_MODEL:-gpt-5.4}" \
  exec - <<'PROMPT'
You are Codex running unattended on the Mac Mini in a dedicated cleanup worktree.

Read AGENTS.md, docs/RUNBOOK.md, and docs/TASKS.md before editing. The current working directory and DAVOSBOT_CLEANUP_RUN_BRANCH identify this run's editable worktree and codex/cleanup-* branch. Stay in this worktree and branch. Do not switch or edit the shared Codex checkout. Treat DAVOSBOT_PROD_DIR (normally /Users/<you>/projects/davosbot) as production runtime: use it only for live change-log export, auto-deploy verification, and smoke tests.

Pull the live phone backlog with:
cd "$DAVOSBOT_PROD_DIR" && venv/bin/python scripts/export_change_log.py --stdout

If the log is empty, stop after a brief report.

For GREEN rows, make the smallest safe fixes. For YELLOW rows, fix only if the change is small, deterministic, and covered by focused tests. Do not touch RED rows except to report them.

Never touch secrets or protected runtime files: .env, MEMORY.md, SOUL.md, gc_state.json, davosbot.db, backups, generated files, exports/private. Do not widen permissions/admin gates, private-send routing, reminder/cron execution, DB schema, tool gates, or live self-edit/deploy behavior.

Run focused tests plus ./scripts/validate.ps1 if PowerShell is available; otherwise run bash scripts/validate.sh. If validation passes, commit with a conventional commit message and push only the current task branch with git push -u origin "$DAVOSBOT_CLEANUP_RUN_BRANCH". Never push master directly. The GitHub fast integrator owns merging eligible GREEN/YELLOW work into master. If it requires review, report that and leave the rows open; do not bypass it.

Wait for the task branch's GitHub Actions to pass, then for the fast integrator to merge it and the resulting master CI to pass. Verify the deployed production commit contains your task commit (the merge SHA can differ from the branch SHA), then run:
cd "$DAVOSBOT_PROD_DIR" && venv/bin/python scripts/runtime_smoke.py

Only after CI, deploy, and runtime smoke pass, close exactly the completed live change-log IDs by running the DavosBot log command helper in production. Use log done #id #id only for rows actually completed. Never run blind log clear.

Report changed files, completed IDs, validation, CI, deployed SHA, runtime smoke, and any remaining RED or unfixed rows.

If blocked or interrupted, leave the worktree and branch intact for a later review. Never stash, reset, force-push, or delete unfinished work.
PROMPT

# Remove only this run's clean worktree after its HEAD has reached master.
# Preserve failed, uncommitted, or unmerged work so a later operator can resume.
git fetch origin master
if [[ -z "$(git status --porcelain)" ]] && git merge-base --is-ancestor HEAD origin/master; then
  cd "$WORK_DIR"
  git worktree remove "$RUN_DIR"
else
  echo "Preserved cleanup worktree for review: $RUN_DIR"
fi

echo "== complete =="
date
