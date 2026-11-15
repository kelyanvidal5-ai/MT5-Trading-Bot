"""Market-regime classification.

A deliberately transparent, rule-based classifier: every label can be traced
to four measurable quantities (trend strength, path efficiency, direction,
volatility rank), which is what makes its decisions explainable and its
behaviour testable. All inputs are causal.

Regimes
-------
TREND_UP / TREND_DOWN  directional, orderly movement
RANGE                  no direction, normal volatility
QUIET                  compressed volatility, no direction
VOLATILE               disorderly / abnormal volatility -> stand aside
UNKNOWN                not enough history
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..core.models import Regime
from ..indicators import ta


@dataclass(frozen=True)
class RegimeParams:
    adx_trend: float = 22.0
    er_trend: float = 0.22
    vol_high: float = 0.93
    vol_low: float = 0.22
    shock_z: float = 6.0
    lookback: int = 250


def robust_z(s: pd.Series, n: int = 100) -> pd.Series:
    """Z-score against the trailing median and MAD (excludes the current bar)."""
    med = s.rolling(n, min_periods=n // 2).median().shift(1)
    mad = (s - med).abs().rolling(n, min_periods=n // 2).median().shift(1)
    return (s - med) / (1.4826 * mad).replace(0.0, np.nan)


def classify(df: pd.DataFrame, params: RegimeParams = RegimeParams()) -> pd.DataFrame:
    """Per-bar regime for an OHLC frame (normally H1)."""
    close = df["close"]
    a = ta.atr(df, 14)
    adx_ = ta.adx(df, 14)["adx"]
    er = ta.efficiency_ratio(close, 20)
    e20, e50 = ta.ema(close, 20), ta.ema(close, 50)
    slope = ta.slope(e50, 10) / a.replace(0.0, np.nan)
    vol_rank = ta.rolling_percentile(a / close, params.lookback)
    shock = robust_z(ta.true_range(df), 100)

    trending = (adx_ >= params.adx_trend) & (er >= params.er_trend)
    up = trending & (e20 > e50) & (slope > 0)
    down = trending & (e20 < e50) & (slope < 0)
    volatile = ((vol_rank >= params.vol_high) & (er < params.er_trend)) | \
               (shock.rolling(3, min_periods=1).max() >= params.shock_z)
    quiet = (vol_rank <= params.vol_low) & (adx_ < params.adx_trend)
    known = adx_.notna() & vol_rank.notna() & e50.notna() & er.notna()

    regime = np.full(len(df), Regime.RANGE.value, dtype=object)
    regime[quiet.values] = Regime.QUIET.value
    regime[up.values] = Regime.TREND_UP.value
    regime[down.values] = Regime.TREND_DOWN.value
    regime[volatile.fillna(False).values] = Regime.VOLATILE.value
    regime[~known.values] = Regime.UNKNOWN.value

    direction = np.where(regime == Regime.TREND_UP.value, 1, np.where(regime == Regime.TREND_DOWN.value, -1, 0))
    return pd.DataFrame({"regime": regime, "direction": direction, "adx": adx_, "efficiency": er,
                         "vol_rank": vol_rank, "slope": slope, "shock_z": shock}, index=df.index)
