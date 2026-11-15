"""Daily report and health checks."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.models import UTC
from . import stats


def daily_report(db, mode: str, risk_snapshot: dict, account: Optional[dict] = None,
                 now: Optional[datetime] = None) -> str:
    """Plain-text end-of-day report built only from recorded data."""
    now = now or datetime.now(UTC)
    s = stats.summary(db, mode, now)
    lines = [f"MT5 Quant daily report - {now:%Y-%m-%d %H:%M} UTC - mode: {mode.upper()}"]
    if account:
        lines.append(f"Balance {account['balance']:.2f} {account['currency']} | equity {account['equity']:.2f} | "
                     f"free margin {account['margin_free']:.2f}")
    pf = s["profit_factor"]
    pf_txt = "n/a" if pf is None else ("inf" if pf == float("inf") else f"{pf:.2f}")
    lines += [
        f"P/L today {s['pnl_today']:+.2f} | week {s['pnl_week']:+.2f} | month {s['pnl_month']:+.2f}",
        f"Trades today {s['trades_today']} | total {s['trades']} | win rate {s['win_rate']:.0%} | "
        f"profit factor {pf_txt} | expectancy {s['expectancy_r']:+.2f}R",
        f"Risk: daily loss {risk_snapshot['daily_loss_pct']:.2f}% | weekly loss "
        f"{risk_snapshot['weekly_loss_pct']:.2f}% | drawdown {risk_snapshot['drawdown_pct']:.2f}% "
        f"(limit {risk_snapshot['limits']['max_drawdown_pct']:.1f}%)",
    ]
    if risk_snapshot["suspended"]:
        lines.append(f"TRADING SUSPENDED: {risk_snapshot['suspend_reason']}")
    if risk_snapshot["kill_switch"].get("active"):
        lines.append(f"EMERGENCY STOP ACTIVE: {risk_snapshot['kill_switch'].get('reason', '')}")
    per = stats.by_strategy(db, mode)
    if per:
        lines.append("Per strategy:")
        for r in per:
            lines.append(f"  {r['strategy']:<18} trades {r['trades']:>4} | net {r['net_profit']:+9.2f} | "
                         f"win {r['win_rate']:.0%} | exp {r['expectancy_r']:+.2f}R")
    statuses = [x for x in db.strategy_statuses() if x["status"] == "suspended"]
    if statuses:
        lines.append("Suspended strategies: " + ", ".join(sorted({x["strategy"] for x in statuses})))
    ok, bad = db.verify_audit_chain()
    lines.append("Audit log integrity: OK" if ok else f"AUDIT LOG INTEGRITY FAILURE at row {bad}")
    return "\n".join(lines)


def write_report(text: str, directory: str | Path, now: Optional[datetime] = None) -> Path:
    now = now or datetime.now(UTC)
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"daily_{now:%Y%m%d}.txt"
    path.write_text(text)
    return path


def health(db, max_age_seconds: float = 120.0) -> dict:
    """Watchdog view: is the engine alive and is its heartbeat fresh?"""
    beats = db.heartbeats()
    engine = beats.get("engine")
    if engine is None:
        return {"alive": False, "status": "never started", "age_seconds": None, "components": beats}
    alive = engine["age_seconds"] <= max_age_seconds and engine["status"] not in ("stopped", "crashed")
    return {"alive": alive, "status": engine["status"], "age_seconds": engine["age_seconds"],
            "details": engine["details"], "components": beats}
