"""Strategy performance evaluation and degradation detection.

Compares what a strategy is doing live against what its validation said it
should do, using R-multiples (profit / initial risk) so results are
comparable across symbols and account sizes.

The monitor can only make trading MORE conservative: it flags a strategy as
WATCH or DEGRADED. The engine suspends DEGRADED strategies and notifies the
operator; re-enabling one is a manual action.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

import numpy as np

HEALTHY, WATCH, DEGRADED, INSUFFICIENT = "healthy", "watch", "degraded", "insufficient_data"
MIN_TRADES = 20


def stats_r(r: Sequence[float]) -> dict:
    a = np.asarray(list(r), float)
    if len(a) == 0:
        return {"trades": 0, "win_rate": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0, "avg_win_r": 0.0,
                "avg_loss_r": 0.0, "max_drawdown_r": 0.0, "max_losing_streak": 0, "total_r": 0.0}
    wins, losses = a[a > 0], a[a <= 0]
    eq = np.cumsum(a)
    dd = float((np.maximum.accumulate(np.maximum(eq, 0.0)) - eq).max())
    streak = best = 0
    for x in a:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    gross_loss = -losses.sum()
    return {"trades": int(len(a)), "win_rate": float(len(wins) / len(a)), "expectancy_r": float(a.mean()),
            "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
            "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss_r": float(losses.mean()) if len(losses) else 0.0, "max_drawdown_r": dd,
            "max_losing_streak": int(best), "total_r": float(a.sum())}


def prob_losing_streak(n: int, k: int, p_loss: float) -> float:
    """P(at least one run of >= k consecutive losses in n independent trades)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    # state = current losing-run length (0..k-1); absorbing once k is reached
    state = np.zeros(k)
    state[0] = 1.0
    hit = 0.0
    for _ in range(n):
        new = np.zeros(k)
        new[0] = state.sum() * (1 - p_loss)
        new[1:] = state[:-1] * p_loss
        hit += state[-1] * p_loss
        state = new
    return float(hit)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class StrategyHealth:
    strategy: str
    status: str
    reasons: list[str] = field(default_factory=list)
    live: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(strategy: str, live_r: Sequence[float], baseline: Optional[dict] = None,
             window: int = 60) -> StrategyHealth:
    """Judge the most recent ``window`` live trades.

    ``baseline`` (from validation): expectancy_r, win_rate, and optionally
    ``mc_dd95_r`` - the 95th-percentile drawdown in R from Monte Carlo.
    """
    recent = list(live_r)[-window:]
    live = stats_r(recent)
    health = StrategyHealth(strategy, HEALTHY, live=live, baseline=dict(baseline or {}))
    n = live["trades"]
    if n < MIN_TRADES:
        health.status = INSUFFICIENT
        health.reasons.append(f"{n} live trades (< {MIN_TRADES}) - no statistical judgement yet")
        return health
    a = np.asarray(recent, float)
    sd = float(a.std(ddof=1)) or 1e-9
    severe: list[str] = []
    mild: list[str] = []

    # 1. Is live expectancy significantly negative?
    t_zero = live["expectancy_r"] / (sd / math.sqrt(n))
    if live["expectancy_r"] < 0 and _norm_cdf(t_zero) < 0.05:
        severe.append(f"live expectancy {live['expectancy_r']:+.2f}R is significantly below zero "
                      f"(t={t_zero:.2f}, n={n})")
    elif live["expectancy_r"] < 0:
        mild.append(f"live expectancy is negative ({live['expectancy_r']:+.2f}R) but not yet significant")

    if baseline:
        base_exp = float(baseline.get("expectancy_r", 0.0))
        base_wr = float(baseline.get("win_rate", 0.5))
        # 2. Is it significantly worse than validation promised?
        t_base = (live["expectancy_r"] - base_exp) / (sd / math.sqrt(n))
        if _norm_cdf(t_base) < 0.01:
            severe.append(f"expectancy {live['expectancy_r']:+.2f}R vs validated {base_exp:+.2f}R (t={t_base:.2f})")
        elif _norm_cdf(t_base) < 0.10:
            mild.append(f"expectancy trailing validation ({live['expectancy_r']:+.2f}R vs {base_exp:+.2f}R)")
        # 3. Losing streak that the validated win rate makes very unlikely.
        p_streak = prob_losing_streak(n, live["max_losing_streak"], 1.0 - base_wr)
        if p_streak < 0.01:
            severe.append(f"losing streak of {live['max_losing_streak']} has probability {p_streak:.2%} "
                          f"at the validated win rate {base_wr:.0%}")
        elif p_streak < 0.05:
            mild.append(f"unusual losing streak of {live['max_losing_streak']} (p={p_streak:.1%})")
        # 4. Drawdown beyond the Monte Carlo envelope.
        dd95 = baseline.get("mc_dd95_r")
        if dd95 and live["max_drawdown_r"] > float(dd95):
            severe.append(f"drawdown {live['max_drawdown_r']:.1f}R exceeds the Monte Carlo 95th percentile "
                          f"({float(dd95):.1f}R)")
    if severe:
        health.status, health.reasons = DEGRADED, severe + mild
    elif mild:
        health.status, health.reasons = WATCH, mild
    return health


def compare(per_strategy_r: dict[str, Sequence[float]]) -> list[dict]:
    """Rank strategies by expectancy with a t-statistic for honesty."""
    rows = []
    for name, r in per_strategy_r.items():
        s = stats_r(r)
        a = np.asarray(list(r), float)
        s["t_stat"] = float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))) if len(a) > 2 and a.std() > 0 else 0.0
        s["strategy"] = name
        rows.append(s)
    return sorted(rows, key=lambda x: x["expectancy_r"], reverse=True)
