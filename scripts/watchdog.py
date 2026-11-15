#!/usr/bin/env python3
"""Watchdog: verify that the engine's heartbeat is fresh.

Run it from cron / Task Scheduler every minute, independently of the engine
process. If the heartbeat is stale it sends a notification (channels from
.env) and exits with status 1 so a supervisor can restart the service.

    python scripts/watchdog.py [--max-age 180]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5quant.core.config import load_config  # noqa: E402
from mt5quant.database.db import Database  # noqa: E402
from mt5quant.monitoring.reports import health  # noqa: E402
from mt5quant.notifications.notifier import Notifier  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=float, default=180.0, help="seconds without heartbeat before alarming")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    db = Database(cfg.resolve_path(cfg.database_path))
    h = health(db, args.max_age)
    if h["alive"]:
        print(f"ok: engine {h['status']}, heartbeat {h['age_seconds']:.0f}s ago")
        return 0
    age = "never" if h["age_seconds"] is None else f"{h['age_seconds']:.0f}s ago"
    msg = (f"Engine heartbeat is stale (status '{h['status']}', last beat {age}). Open positions keep their "
           "broker-side stop-loss and take-profit, but nothing is being managed or opened.")
    Notifier.from_env(db).notify("critical", "Trading engine is not running", msg, key="watchdog")
    print("ALERT: " + msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
