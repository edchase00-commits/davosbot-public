"""Send the owner a cleanup prompt when DavosBot's phone change log has rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from davosbot import commands
from davosbot.config import OWNER_ID
from davosbot.imessage import send_message


DEFAULT_STATE_PATH = ROOT / ".auto_deploy" / "cleanup_monitor_state.json"


def _fingerprint(rows) -> str:
    payload = [
        {
            "id": int(row[0]),
            "request": str(row[1] or ""),
            "reason": str(row[2] or ""),
        }
        for row in rows
    ]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def build_message(rows, *, max_rows: int = 5) -> str:
    buckets = commands._bucket_change_log_rows(rows)
    counts = {color: len(items) for color, items in buckets.items()}
    lines = [
        f"Davos cleanup monitor: GREEN {counts['green']} | YELLOW {counts['yellow']} | RED {counts['red']}",
        "Top rows:",
    ]
    for row in rows[:max_rows]:
        lines.append(commands._format_bucket_item(row))
    lines.extend([
        "",
        "Reply `yes fix` and I will run Codex cleanup on the Mini now, then text when it finishes.",
        "Text `master prompt` if you want the pasteable phone-Codex handoff instead.",
        "Nightly 3am still runs as a fallback and clears only completed IDs.",
    ])
    return "\n".join(lines)


def maybe_send_cleanup_dm(*, state_path: Path, repeat_hours: float, dry_run: bool = False, force: bool = False) -> str:
    rows = commands._fetch_change_log_rows()
    if not rows:
        _save_state(state_path, {"last_empty_ts": time.time()})
        return "empty"

    fingerprint = _fingerprint(rows)
    state = _load_state(state_path)
    now = time.time()
    last_sent_ts = float(state.get("last_sent_ts") or 0)
    if (
        not force
        and state.get("last_fingerprint") == fingerprint
        and now - last_sent_ts < repeat_hours * 3600
    ):
        return "unchanged"

    message = build_message(rows)
    if dry_run:
        print(message)
        return "dry-run"

    if not OWNER_ID:
        return "missing-owner"
    ok = send_message(OWNER_ID, message)
    if not ok:
        return "send-failed"

    _save_state(
        state_path,
        {
            "last_fingerprint": fingerprint,
            "last_sent_ts": now,
            "last_row_count": len(rows),
        },
    )
    return "sent"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--repeat-hours", type=float, default=6.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    result = maybe_send_cleanup_dm(
        state_path=Path(args.state_path),
        repeat_hours=args.repeat_hours,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(result)
    return 2 if result == "send-failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
