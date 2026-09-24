#!/usr/bin/env bash
set -euo pipefail

PROD_DIR="${DAVOSBOT_PROD_DIR:-/Users/<you>/projects/davosbot}"
PYTHON_BIN="${DAVOSBOT_PYTHON:-$PROD_DIR/venv/bin/python}"
CLEANUP_MONITOR_ENABLED="${DAVOSBOT_CLEANUP_MONITOR_ENABLED:-false}"
MARKER_BEGIN="# BEGIN davosbot-managed-crons"
MARKER_END="# END davosbot-managed-crons"

if [[ ! -d "$PROD_DIR" ]]; then
  echo "Missing production directory: $PROD_DIR" >&2
  exit 1
fi

tmp_current="$(mktemp)"
tmp_next="$(mktemp)"
trap 'rm -f "$tmp_current" "$tmp_next"' EXIT

crontab -l >"$tmp_current" 2>/dev/null || true

python3 - "$tmp_current" "$tmp_next" "$MARKER_BEGIN" "$MARKER_END" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
begin = sys.argv[3]
end = sys.argv[4]

lines = source.read_text(encoding="utf-8").splitlines()
out: list[str] = []
skip = False
skip_next = False
legacy_tags = {
    "# davosbot-cleanup-monitor",
    "# davosbot-nightly-safe-cleanup",
    "# davosbot-maintenance-diagnostics",
    "# davosbot-quality-sweep",
}
for line in lines:
    stripped = line.strip()
    if stripped == begin:
        skip = True
        continue
    if skip:
        if stripped == end:
            skip = False
        continue
    if skip_next:
        skip_next = False
        continue
    if stripped in legacy_tags:
        skip_next = True
        continue
    if (
        "git add MEMORY.md" in stripped
        and "auto: memory backup" in stripped
        and "--allow-empty" in stripped
    ):
        continue
    out.append(line)

target.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY

cleanup_monitor_line=""
case "$CLEANUP_MONITOR_ENABLED" in
    1|true|TRUE|yes|YES|on|ON)
        cleanup_monitor_line="0 * * * * cd $PROD_DIR && $PYTHON_BIN scripts/cleanup_monitor_dm.py >> $PROD_DIR/.auto_deploy/cleanup_monitor_cron.log 2>&1"
        ;;
esac

cat >>"$tmp_next" <<EOF
$MARKER_BEGIN
$cleanup_monitor_line
15 * * * * cd $PROD_DIR && $PYTHON_BIN scripts/maintenance_diagnostics.py --update-state >> $PROD_DIR/.auto_deploy/maintenance_diagnostics_cron.log 2>&1
45 * * * * cd $PROD_DIR && $PYTHON_BIN scripts/quality_sweep.py --mode light --fix >> $PROD_DIR/.auto_deploy/quality_sweep_cron.log 2>&1
30 2 * * * cd $PROD_DIR && $PYTHON_BIN scripts/quality_sweep.py --mode full --fix >> $PROD_DIR/.auto_deploy/quality_sweep_cron.log 2>&1
0 3 * * * cd $PROD_DIR && /bin/bash scripts/nightly_safe_cleanup_codex.sh >> $PROD_DIR/.auto_deploy/nightly_safe_cleanup_cron.log 2>&1
$MARKER_END
EOF

crontab "$tmp_next"
echo "Installed DavosBot managed crons for $PROD_DIR"
if [[ -n "$cleanup_monitor_line" ]]; then
    echo "Cleanup monitor DM: enabled"
else
    echo "Cleanup monitor DM: disabled (opt in with DAVOSBOT_CLEANUP_MONITOR_ENABLED=true)"
fi
