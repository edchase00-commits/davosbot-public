"""Inspect the official release; --run enables the authorized Mini reminder watch."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from davosbot import platinum_emerald_watch as watch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    try:
        now = datetime.now(timezone.utc)
        if not args.run and not args.status:
            print(json.dumps(watch.observe(now.date()), sort_keys=True))
            return 0
        from davosbot.config import PROJECT_ROOT
        state = PROJECT_ROOT / ".auto_deploy" / "platinum_emerald_watch.json"
        if args.status:
            print(json.dumps(watch.load_state(state), sort_keys=True))
            return 0
        if sys.platform != "darwin":
            raise watch.WatchError("mini_only")
        with watch.watch_lock(state.with_suffix(".lock")):
            observation = watch.observe(now.date())
            queue, pending = watch.native_reminders()
            result = watch.process_observation(state, observation, now, queue, pending)
        print(result)
        return 2 if result == "needs_review" else 0
    except watch.WatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("watch_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
