"""Recurring-pattern research on historical data.

Measures whether simple recurring conditions (hour of day, day of week,
candlestick patterns) were followed by returns that differ from zero, with
t-statistics and Benjamini-Hochberg control of the false discovery rate -
when dozens of patterns are tested, some look significant by luck alone.

This module produces research reports only. Nothing found here is traded
automatically: a pattern becomes tradable only as a strategy that passes the
full validation pipeline.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..indicators.candles import candle_patterns

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _p_two_sided(t: float) -> float:
    return float(math.erfc(abs(t) / math.sqrt(2.0)))


def benjamini_hochberg(p: np.ndarray, alpha: float = 0.05) -> np.ndarray:
    """Boolean mask of discoveries at FDR ``alpha``."""
    p = np.asarray(p, float)
    order = np.argsort(p)
    thresh = alpha * (np.arange(1, len(p) + 1) / max(len(p), 1))
    passed = p[order] <= thresh
    out = np.zeros(len(p), dtype=bool)
    if passed.any():
        out[order[: np.flatnonzero(passed).max() + 1]] = True
    return out


def _row(kind: str, label: str, ret: pd.Series) -> dict:
    n = int(ret.count())
    mean, sd = float(ret.mean()) if n else 0.0, float(ret.std(ddof=1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else 0.0
    return {"kind": kind, "pattern": label, "n": n, "mean_return_bps": mean * 1e4,
            "hit_rate": float((ret > 0).mean()) if n else 0.0, "t_stat": t, "p_value": _p_two_sided(t)}


def mine(df: pd.DataFrame, horizon: int = 4, min_obs: int = 50, alpha: float = 0.05) -> pd.DataFrame:
    """Test recurring conditions against the next ``horizon``-bar return.

    The forward return is a LABEL used for research statistics only; it is
    never exposed as a feature."""
    fwd = df["close"].shift(-horizon) / df["close"] - 1.0
    rows: list[dict] = []
    for h in range(24):
        r = fwd[df.index.hour == h].dropna()
        if len(r) >= min_obs:
            rows.append(_row("hour_utc", f"{h:02d}:00", r))
    for d in range(7):
        r = fwd[df.index.dayofweek == d].dropna()
        if len(r) >= min_obs:
            rows.append(_row("weekday", _DAYS[d], r))
    pats = candle_patterns(df)
    for col in [c for c in pats.columns if pats[c].dtype == bool]:
        r = fwd[pats[col]].dropna()
        if len(r) >= min_obs:
            rows.append(_row("candle", col, r))
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["significant_after_fdr"] = benjamini_hochberg(out["p_value"].values, alpha)
    return out.sort_values("p_value").reset_index(drop=True)
