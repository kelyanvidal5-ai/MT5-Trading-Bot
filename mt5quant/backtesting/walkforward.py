"""Walk-forward analysis and parameter-sensitivity testing.

Walk-forward: repeatedly (1) choose parameters on an in-sample window from
a small pre-declared grid, (2) trade the NEXT, unseen window with them. The
stitched out-of-sample segments are the only numbers that count; in-sample
results are kept solely to measure how much performance survives (the
walk-forward efficiency).

The optimisation objective is a t-statistic-like score (expectancy in R x
sqrt(trades)) with a minimum trade count, which rewards consistency and
sample size rather than the single most impressive curve.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..core.models import ClosedTrade
from ..market_data.timeframes import MarketData
from ..strategies.base import Strategy
from ..strategies.registry import STRATEGIES, create
from .costs import CostModel
from .engine import Backtester, BacktestResult, BacktestSettings

MIN_IS_TRADES = 15


def grid_combinations(grid: dict[str, list]) -> list[dict]:
    keys = sorted(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def objective(result: BacktestResult, min_trades: int = MIN_IS_TRADES) -> float:
    r = result.r_multiples
    if len(r) < min_trades:
        return -math.inf
    return float(r.mean() * math.sqrt(len(r)))


@dataclass
class Window:
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_end: pd.Timestamp
    params: dict
    is_expectancy_r: float
    is_trades: int
    oos_expectancy_r: float
    oos_trades: int
    oos_net: float


@dataclass
class WalkForwardResult:
    strategy: str
    windows: list[Window] = field(default_factory=list)
    oos_trades: list[ClosedTrade] = field(default_factory=list)
    oos_event_ids: list[int] = field(default_factory=list)
    oos_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    combos_tested: int = 0
    backtesters: dict = field(default_factory=dict, repr=False)

    @property
    def efficiency(self) -> float:
        """OOS expectancy as a fraction of IS expectancy (trade-weighted)."""
        w = [x for x in self.windows if x.is_trades > 0]
        is_n = sum(x.is_trades for x in w)
        oos_n = sum(x.oos_trades for x in w)
        if not is_n or not oos_n:
            return 0.0
        is_exp = sum(x.is_expectancy_r * x.is_trades for x in w) / is_n
        oos_exp = sum(x.oos_expectancy_r * x.oos_trades for x in w) / oos_n
        return float(oos_exp / is_exp) if is_exp > 0 else 0.0

    @property
    def profitable_windows(self) -> float:
        w = [x for x in self.windows if x.oos_trades > 0]
        return float(np.mean([x.oos_net > 0 for x in w])) if w else 0.0

    @property
    def last_params(self) -> dict:
        return dict(self.windows[-1].params) if self.windows else {}

    def summary(self) -> dict:
        return {"windows": len(self.windows), "efficiency": self.efficiency,
                "profitable_windows": self.profitable_windows, "combos_tested": self.combos_tested,
                "oos_trades": len(self.oos_trades),
                "window_table": [{"is": f"{w.is_start:%Y-%m-%d}..{w.is_end:%Y-%m-%d}",
                                  "oos_until": f"{w.oos_end:%Y-%m-%d}", "params": w.params,
                                  "is_exp_r": round(w.is_expectancy_r, 3), "is_trades": w.is_trades,
                                  "oos_exp_r": round(w.oos_expectancy_r, 3), "oos_trades": w.oos_trades,
                                  "oos_net": round(w.oos_net, 2)} for w in self.windows]}


def walk_forward(data: dict[str, MarketData], base_tf: str, strategy_name: str, cost: CostModel,
                 settings: Optional[BacktestSettings] = None, start: Optional[pd.Timestamp] = None,
                 end: Optional[pd.Timestamp] = None, train_days: int = 120, test_days: int = 40,
                 anchored: bool = False, grid: Optional[dict[str, list]] = None,
                 fixed_params: Optional[dict] = None,
                 progress: Optional[Callable[[str], None]] = None) -> WalkForwardResult:
    cls = STRATEGIES[strategy_name]
    combos = grid_combinations(grid if grid is not None else cls.grid) or [{}]
    fixed = fixed_params or {}
    first = min(md.frame(base_tf).index[0] for md in data.values())
    last = max(md.frame(base_tf).index[-1] for md in data.values())
    start = start or first
    end = end or last + pd.Timedelta(minutes=1)
    out = WalkForwardResult(strategy_name, combos_tested=len(combos))
    # One backtester per parameter set: signals are generated once (they are
    # causal), every window is then just a slice of that event list.
    testers = {i: Backtester(data, base_tf, [create(strategy_name, {**fixed, **c})], cost, settings)
               for i, c in enumerate(combos)}
    out.backtesters = testers
    train, test = pd.Timedelta(days=train_days), pd.Timedelta(days=test_days)
    is_start, is_end = start, start + train
    balance = (settings or BacktestSettings()).initial_balance
    pieces = []
    while is_end + pd.Timedelta(days=5) < end:
        oos_end = min(is_end + test, end)
        scores = {}
        for i, bt in testers.items():
            scores[i] = (objective(bt.run(is_start, is_end)), i)
        best_score, best = max(scores.values())
        if best_score == -math.inf:
            best = 0                                   # nothing qualified: fall back to the first grid point
        is_res = testers[best].run(is_start, is_end)
        oos_res = testers[best].run(is_end, oos_end)
        r_is, r_oos = is_res.r_multiples, oos_res.r_multiples
        out.windows.append(Window(is_start, is_end, oos_end, combos[best],
                                  float(r_is.mean()) if len(r_is) else 0.0, len(r_is),
                                  float(r_oos.mean()) if len(r_oos) else 0.0, len(r_oos),
                                  float(sum(t.profit for t in oos_res.trades))))
        out.oos_trades.extend(oos_res.trades)
        out.oos_event_ids.extend((best, int(t.client_id)) for t in oos_res.trades)
        if len(oos_res.equity):
            pieces.append(oos_res.equity - oos_res.initial_balance)
        if progress:
            progress(f"{strategy_name}: window {len(out.windows)} OOS {r_oos.mean() if len(r_oos) else 0:+.2f}R "
                     f"({len(r_oos)} trades) params={combos[best]}")
        is_end = oos_end
        if not anchored:
            is_start = is_end - train
    # Stitch OOS P/L segments into one continuous equity curve.
    level, stitched = balance, []
    for seg in pieces:
        stitched.append(seg + level)
        level = float(stitched[-1].iloc[-1])
    if stitched:
        eq = pd.concat(stitched)
        out.oos_equity = eq[~eq.index.duplicated(keep="last")].sort_index()
    return out


# ------------------------------------------------------------------ sensitivity
def perturbations(strategy: Strategy, steps: tuple[float, ...] = (-0.2, -0.1, 0.1, 0.2)) -> list[dict]:
    """One-at-a-time +/-10% and +/-20% changes of every numeric parameter."""
    out = []
    for key, value in strategy.params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or key == "min_rr":
            continue
        for s in steps:
            new = value * (1.0 + s)
            if isinstance(value, int):
                new = int(round(new))
                if new == value:
                    new = value + (1 if s > 0 else -1)
                if new <= 0:
                    continue
            out.append({"param": key, "change": s, "params": {**strategy.params, key: new}})
    return out


def sensitivity(data: dict[str, MarketData], base_tf: str, strategy_name: str, params: dict, cost: CostModel,
                settings: Optional[BacktestSettings] = None, start: Optional[pd.Timestamp] = None,
                end: Optional[pd.Timestamp] = None) -> dict:
    """How fragile is the result to small parameter changes? A robust edge
    degrades gracefully; an overfit one collapses next to its optimum."""
    base_strategy = create(strategy_name, params)
    base = Backtester(data, base_tf, [base_strategy], cost, settings).run(start, end)
    base_r = base.r_multiples
    base_exp = float(base_r.mean()) if len(base_r) else 0.0
    rows = []
    for p in perturbations(base_strategy):
        res = Backtester(data, base_tf, [create(strategy_name, p["params"])], cost, settings).run(start, end)
        r = res.r_multiples
        rows.append({"param": p["param"], "change": p["change"], "trades": int(len(r)),
                     "expectancy_r": float(r.mean()) if len(r) else 0.0})
    exps = np.array([x["expectancy_r"] for x in rows]) if rows else np.array([])
    return {
        "base_expectancy_r": base_exp, "base_trades": int(len(base_r)), "neighbours": len(rows),
        "profitable_fraction": float((exps > 0).mean()) if len(exps) else 0.0,
        "worst_expectancy_r": float(exps.min()) if len(exps) else 0.0,
        "median_expectancy_r": float(np.median(exps)) if len(exps) else 0.0,
        "retained_fraction": float(np.median(exps) / base_exp) if base_exp > 0 and len(exps) else 0.0,
        "table": rows,
    }
