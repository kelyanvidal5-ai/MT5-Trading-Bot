"""Account and strategy statistics computed from the trade history and the
equity snapshots stored in the database. Used by the dashboard, the daily
report and the performance monitor."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..ai_engine.performance import stats_r
from ..core.models import UTC


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def trade_stats(trades: list[dict]) -> dict:
    """``trades`` are rows from the trades table (any order)."""
    n = len(trades)
    wins = [t["profit"] for t in trades if t["profit"] > 0]
    losses = [t["profit"] for t in trades if t["profit"] <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    r = [t["r_multiple"] for t in sorted(trades, key=lambda t: t["time_close"]) if t["risk_amount"] > 0]
    rs = stats_r(r)
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n if n else 0.0,
        "net_profit": gross_win - gross_loss, "gross_profit": gross_win, "gross_loss": gross_loss,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        "payoff_ratio": (avg_win / -avg_loss) if avg_loss < 0 else None,
        "expectancy": (gross_win - gross_loss) / n if n else 0.0,
        "expectancy_r": rs["expectancy_r"], "max_losing_streak": rs["max_losing_streak"],
        "commission": sum(t.get("commission", 0.0) for t in trades),
        "swap": sum(t.get("swap", 0.0) for t in trades),
    }


def max_drawdown(equity: list[float]) -> tuple[float, float]:
    """(max drawdown in currency, max drawdown in % of the running peak)."""
    peak, dd_abs, dd_pct = float("-inf"), 0.0, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd_abs = max(dd_abs, peak - e)
            dd_pct = max(dd_pct, (peak - e) / peak * 100.0)
    return dd_abs, dd_pct


def period_pnl(trades: list[dict], since: datetime) -> float:
    return sum(t["profit"] for t in trades if _parse(t["time_close"]) >= since)


def summary(db, mode: str, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(UTC)
    trades = db.trades(mode=mode)
    curve = db.equity_curve(mode=mode, max_points=5000)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    dd_abs, dd_pct = max_drawdown([p["equity"] for p in curve])
    out = trade_stats(trades)
    out.update({
        "pnl_today": period_pnl(trades, day), "pnl_week": period_pnl(trades, week),
        "pnl_month": period_pnl(trades, month),
        "trades_today": sum(1 for t in trades if _parse(t["time_close"]) >= day),
        "max_drawdown": dd_abs, "max_drawdown_pct": dd_pct,
        "first_equity": curve[0]["equity"] if curve else None,
        "last_equity": curve[-1]["equity"] if curve else None,
    })
    return out


def by_strategy(db, mode: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for t in db.trades(mode=mode):
        groups.setdefault(t["strategy"] or "unknown", []).append(t)
    rows = []
    for name, ts in groups.items():
        s = trade_stats(ts)
        s["strategy"] = name
        rows.append(s)
    return sorted(rows, key=lambda r: r["net_profit"], reverse=True)


def daily_pnl(db, mode: str, days: int = 30, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets: dict[str, float] = {}
    for t in db.trades(mode=mode, since=start):
        key = t["time_close"][:10]
        buckets[key] = buckets.get(key, 0.0) + t["profit"]
    return [{"date": k, "pnl": v} for k, v in sorted(buckets.items())]
