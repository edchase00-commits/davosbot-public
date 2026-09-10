#!/usr/bin/env python3
"""Inspect browser availability or perform local, interactive merchant sign-in."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capability", "connect"))
    parser.add_argument("service", choices=("doordash", "ubereats"))
    parser.add_argument("--actor", help="Authorized sender; defaults to the configured owner")
    args = parser.parse_args(argv)
    from davosbot.config import OWNER_ID
    from davosbot import checkout_browser
    actor = args.actor or OWNER_ID
    if args.command == "connect" and not sys.stdin.isatty():
        parser.error("connect requires an interactive local terminal and visible browser")
    result = getattr(checkout_browser, args.command)(actor, args.service)
    # These routines return availability metadata only, never profile paths or snapshots.
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
