"""Candlestick pattern recognition (causal: each pattern is known at the
close of its final candle)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .ta import atr


def candle_patterns(df: pd.DataFrame, atr_n: int = 14) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).replace(0.0, np.nan)
    body = (c - o).abs()
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    a = atr(df, atr_n)
    bull, bear = c > o, c < o
    po, pc = o.shift(1), c.shift(1)
    p_body = (pc - po).abs()

    out = pd.DataFrame(index=df.index)
    out["doji"] = body <= 0.1 * rng
    out["bull_engulfing"] = bull & (pc < po) & (c >= po) & (o <= pc) & (body > p_body)
    out["bear_engulfing"] = bear & (pc > po) & (c <= po) & (o >= pc) & (body > p_body)
    # Pin bars: long rejection wick, small body at the far end of the range.
    out["hammer"] = (lower >= 0.6 * rng) & (body <= 0.3 * rng) & (upper <= 0.2 * rng) & (rng >= 0.7 * a)
    out["shooting_star"] = (upper >= 0.6 * rng) & (body <= 0.3 * rng) & (lower <= 0.2 * rng) & (rng >= 0.7 * a)
    out["inside_bar"] = (h <= h.shift(1)) & (l >= l.shift(1))
    out["outside_bar"] = (h > h.shift(1)) & (l < l.shift(1))
    # Three-candle reversals.
    b2, b1 = (c.shift(2) - o.shift(2)), p_body
    small_mid = b1 <= 0.5 * b2.abs()
    out["morning_star"] = (b2 < 0) & small_mid & bull & (c > (o.shift(2) + c.shift(2)) / 2.0) & \
                          (b2.abs() >= 0.6 * a)
    out["evening_star"] = (b2 > 0) & small_mid & bear & (c < (o.shift(2) + c.shift(2)) / 2.0) & \
                          (b2.abs() >= 0.6 * a)
    # Momentum (displacement) candles: large body closing near the extreme.
    out["bull_momentum"] = bull & (body >= 0.65 * rng) & (rng >= 1.0 * a)
    out["bear_momentum"] = bear & (body >= 0.65 * rng) & (rng >= 1.0 * a)
    out = out.fillna(False).astype(bool)
    out["bull_score"] = (out["bull_engulfing"].astype(int) + out["hammer"].astype(int) +
                         out["morning_star"].astype(int) + out["bull_momentum"].astype(int))
    out["bear_score"] = (out["bear_engulfing"].astype(int) + out["shooting_star"].astype(int) +
                         out["evening_star"].astype(int) + out["bear_momentum"].astype(int))
    return out


def rejection(df: pd.DataFrame, wick_frac: float = 0.55) -> pd.DataFrame:
    """Wick rejections: +1 bullish (long lower wick, close in upper half),
    -1 bearish."""
    rng = (df["high"] - df["low"]).replace(0.0, np.nan)
    lower = np.minimum(df["open"], df["close"]) - df["low"]
    upper = df["high"] - np.maximum(df["open"], df["close"])
    pos = (df["close"] - df["low"]) / rng
    bull = (lower >= wick_frac * rng) & (pos >= 0.5)
    bear = (upper >= wick_frac * rng) & (pos <= 0.5)
    return pd.DataFrame({"bull": bull.fillna(False), "bear": bear.fillna(False)})
