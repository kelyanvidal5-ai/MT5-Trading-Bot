"""Performance metrics for a backtest (or any trade list + equity curve)."""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from ..ai_engine.performance import stats_r
from .engine import BacktestResult

TRADING_DAYS = 252


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation)."""
    p = min(max(p, 1e-12), 1 - 1e-12)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def daily_returns(equity: pd.Series) -> pd.Series:
    """Business-day returns from an irregular equity series."""
    if len(equity) < 2:
        return pd.Series(dtype=float)
    daily = equity.resample("1D").last().ffill()
    daily = daily[daily.index.dayofweek < 5]
    return daily.pct_change().dropna()


def drawdown(equity: pd.Series) -> tuple[float, float, float]:
    """(max drawdown %, max drawdown in currency, longest drawdown in days)."""
    if equity.empty:
        return 0.0, 0.0, 0.0
    peak = equity.cummax()
    dd = (peak - equity)
    pct = float((dd / peak).max() * 100.0)
    under = dd > 0
    longest, start = 0.0, None
    for t, u in zip(equity.index, under.values):
        if u and start is None:
            start = t
        elif not u and start is not None:
            longest = max(longest, (t - start) / pd.Timedelta(days=1))
            start = None
    if start is not None:
        longest = max(longest, (equity.index[-1] - start) / pd.Timedelta(days=1))
    return pct, float(dd.max()), float(longest)


def probabilistic_sharpe(returns: np.ndarray, benchmark_sr: float = 0.0) -> float:
    """P(true Sharpe > benchmark) given sample length, skew and kurtosis
    (Bailey & Lopez de Prado). Sharpe ratios here are per-period."""
    n = len(returns)
    if n < 10 or returns.std(ddof=1) == 0:
        return 0.0
    sr = returns.mean() / returns.std(ddof=1)
    z = (returns - returns.mean()) / returns.std(ddof=1)
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr))
    return _norm_cdf((sr - benchmark_sr) * math.sqrt(n - 1) / denom)


def deflated_sharpe(returns: np.ndarray, n_trials: int, trials_sr_std: Optional[float] = None) -> float:
    """Probabilistic Sharpe against the Sharpe expected from the BEST of
    ``n_trials`` skill-less strategies - corrects for having tried many
    parameter sets and kept the winner."""
    n = len(returns)
    if n < 10 or returns.std(ddof=1) == 0:
        return 0.0
    if n_trials <= 1:
        return probabilistic_sharpe(returns, 0.0)
    sd = trials_sr_std if trials_sr_std is not None else 1.0 / math.sqrt(n - 1)
    gamma = 0.5772156649
    expected_max = sd * ((1 - gamma) * _norm_ppf(1 - 1.0 / n_trials) + gamma * _norm_ppf(1 - 1.0 / (n_trials * math.e)))
    return probabilistic_sharpe(returns, expected_max)


def compute(result: BacktestResult, n_trials: int = 1) -> dict:
    """All headline metrics for one backtest."""
    trades = result.trades
    eq = result.equity
    r = result.r_multiples
    profits = np.array([t.profit for t in trades], dtype=float)
    wins, losses = profits[profits > 0], profits[profits <= 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    net = float(profits.sum())
    rets = daily_returns(eq)
    days = max((eq.index[-1] - eq.index[0]) / pd.Timedelta(days=1), 1.0) if len(eq) > 1 else 1.0
    years = days / 365.25
    final = float(eq.iloc[-1]) if len(eq) else result.initial_balance
    total_ret = final / result.initial_balance - 1.0
    cagr = (final / result.initial_balance) ** (1 / years) - 1.0 if years > 0.05 and final > 0 else total_ret
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    downside = float(np.sqrt((np.minimum(rets.values, 0.0) ** 2).mean())) if len(rets) else 0.0
    sharpe = float(rets.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    sortino = float(rets.mean() / downside * math.sqrt(TRADING_DAYS)) if downside > 0 else 0.0
    dd_pct, dd_abs, dd_days = drawdown(eq)
    rs = stats_r(r)
    t_stat = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 and r.std(ddof=1) > 0 else 0.0
    planned_rr = [abs(t.tp - t.price_open) / abs(t.price_open - t.sl) for t in trades
                  if t.sl and t.tp and abs(t.price_open - t.sl) > 0]
    hold = [(t.time_close - t.time_open).total_seconds() / 3600.0 for t in trades]
    return {
        "trades": int(len(trades)),
        "net_profit": net,
        "return_pct": total_ret * 100.0,
        "cagr_pct": cagr * 100.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "win_rate": float(len(wins) / len(profits)) if len(profits) else 0.0,
        "expectancy": net / len(profits) if len(profits) else 0.0,
        "expectancy_r": rs["expectancy_r"],
        "t_stat_r": t_stat,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_realised_rr": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0
        else 0.0,
        "avg_planned_rr": float(np.mean(planned_rr)) if planned_rr else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": dd_pct,
        "max_drawdown": dd_abs,
        "longest_drawdown_days": dd_days,
        "calmar": (cagr * 100.0 / dd_pct) if dd_pct > 0 else 0.0,
        "max_losing_streak": rs["max_losing_streak"],
        "max_drawdown_r": rs["max_drawdown_r"],
        "avg_hold_hours": float(np.mean(hold)) if hold else 0.0,
        "trades_per_month": len(trades) / max(days / 30.44, 1e-9),
        "time_in_market_pct": result.bars_in_market / result.bars_total * 100.0 if result.bars_total else 0.0,
        "commission_paid": float(-sum(t.commission for t in trades)),
        "swap_paid": float(-sum(t.swap for t in trades)),
        "psr": probabilistic_sharpe(rets.values) if len(rets) else 0.0,
        "deflated_sharpe": deflated_sharpe(rets.values, n_trials) if len(rets) else 0.0,
        "signals_total": result.signals_total,
        "signals_taken": result.signals_taken,
        "days": float(days),
        "flags": list(result.flags),
        "synthetic_data": result.synthetic,
    }


def by_strategy(result: BacktestResult) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in result.trades:
        out.setdefault(t.strategy, []).append(t)          # type: ignore[arg-type]
    table = {}
    for name, ts in out.items():
        r = np.array([t.r_multiple for t in ts if t.risk_amount > 0])
        s = stats_r(r)
        s["net_profit"] = float(sum(t.profit for t in ts))
        table[name] = s
    return table
