#!/usr/bin/env python3
"""Run the fixed ESPN parlay alert monitor as a standalone PM2 process."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from davosbot.config import DB_PATH  # noqa: E402
from davosbot.espn_parlay import (  # noqa: E402
    TRACKED_EVENTS,
    fetch_snapshot,
    format_alert,
    is_settled,
    is_terminal_boundary,
)
from davosbot.live_score_delivery import LiveScoreDelivery, default_state_dir  # noqa: E402
from davosbot.live_score_monitor import LiveScoreMonitor, MonitorError  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor the fixed Nov. 20-21 ESPN parlay and deliver native Messages alerts.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="strict live-score delivery config JSON (default: STATE_DIR/config.json)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="private external monitor state directory",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll every tracked event once, print status JSON, and exit",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="preflight provider matching/formatting without claiming or sending alerts (requires --once)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.no_send and not args.once:
        print("--no-send requires --once", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state_dir = args.state_dir or default_state_dir()
    config_path = args.config or state_dir / "config.json"
    try:
        with LiveScoreDelivery(
            state_dir,
            DB_PATH,
            config_path=config_path,
        ) as delivery:
            monitor = LiveScoreMonitor(
                TRACKED_EVENTS,
                fetch_snapshot,
                format_alert,
                is_settled,
                delivery,
                state_dir / "monitor-state.json",
                state_dir / "status.json",
                is_terminal_boundary=is_terminal_boundary,
                expected_event_count=12,
                expected_pick_count=14,
            )
            if args.once:
                result = monitor.run_once(no_send=args.no_send, force_poll=True)
                print(json.dumps(result.status, indent=2, sort_keys=True, ensure_ascii=False))
                if result.status["unmatched_events"]:
                    return 1
                if any(event.get("last_error") for event in result.status["events"].values()):
                    return 1
                return 0
            monitor.run_forever()
            return 0
    except Exception as exc:
        value = str(exc).strip().lower().replace(" ", "_")
        if not value or len(value) > 160 or not all(char.isalnum() or char in "_.:-" for char in value):
            value = type(exc).__name__
        print(value, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
