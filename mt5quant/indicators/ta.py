"""Classic technical indicators (vectorised, strictly causal).

Every function uses only information available at the close of each bar:
the value at row ``i`` never depends on rows ``> i``. This property is
enforced by ``tests/test_no_lookahead.py`` for every indicator.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..market_data.timeframes import _trading_period_open

EMA_PERIODS = (9, 20, 50, 100, 200)


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (RMA)."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain, loss = wilder(delta.clip(lower=0.0), n), wilder((-delta).clip(lower=0.0), n)
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.where(loss != 0.0, 100.0).where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower) / mid
    pctb = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width, "pctb": pctb})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up, down = df["high"].diff(), -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = wilder(true_range(df), n).replace(0.0, np.nan)
    plus_di = 100.0 * wilder(plus_dm, n) / tr
    minus_di = 100.0 * wilder(minus_dm, n) / tr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": wilder(dx, n), "plus_di": plus_di,
                         "minus_di": minus_di})


def vwap(df: pd.DataFrame, bands: float = 2.0) -> pd.DataFrame:
    """Volume-weighted average price anchored at the start of each FX trading
    day (17:00 New York), with volume-weighted standard-deviation bands."""
    day = _trading_period_open(df.index, weekly=False)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].clip(lower=1.0)
    g = pd.Series(day, index=df.index)
    cum_v = vol.groupby(g).cumsum()
    cum_pv = (tp * vol).groupby(g).cumsum()
    cum_pv2 = (tp * tp * vol).groupby(g).cumsum()
    v = cum_pv / cum_v
    var = (cum_pv2 / cum_v - v * v).clip(lower=0.0)
    sd = np.sqrt(var)
    return pd.DataFrame({"vwap": v, "upper": v + bands * sd, "lower": v - bands * sd, "sd": sd})


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Channel of the PREVIOUS ``n`` bars (the current bar is excluded so a
    close beyond the channel is a genuine breakout)."""
    upper = df["high"].rolling(n, min_periods=n).max().shift(1)
    lower = df["low"].rolling(n, min_periods=n).min().shift(1)
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2.0})


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 1.5) -> pd.DataFrame:
    mid = ema(df["close"], n)
    rng = atr(df, n)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * rng, "lower": mid - mult * rng})


def roc(close: pd.Series, n: int = 10) -> pd.Series:
    return close / close.shift(n) - 1.0


def zscore(s: pd.Series, n: int = 50) -> pd.Series:
    mean = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return (s - mean) / sd.replace(0.0, np.nan)


def efficiency_ratio(close: pd.Series, n: int = 20) -> pd.Series:
    """Kaufman efficiency ratio: |net move| / path length. ~1 trend, ~0 noise."""
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n, min_periods=n).sum()
    return net / path.replace(0.0, np.nan)


def rolling_percentile(s: pd.Series, n: int = 100) -> pd.Series:
    """Rank (0..1) of the current value within its trailing window."""
    return s.rolling(n, min_periods=n).rank(pct=True)


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    return volume / volume.rolling(n, min_periods=n).mean().shift(1).replace(0.0, np.nan)


def obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff()).fillna(0.0) * df["volume"]).cumsum()


def slope(s: pd.Series, n: int = 5) -> pd.Series:
    """Average per-bar change over the last ``n`` bars."""
    return (s - s.shift(n)) / n


def ema_ribbon(close: pd.Series, periods: tuple[int, ...] = EMA_PERIODS) -> pd.DataFrame:
    return pd.DataFrame({f"ema{p}": ema(close, p) for p in periods})


def ema_alignment(ribbon: pd.DataFrame) -> pd.Series:
    """+1 when EMAs are stacked bullishly (fast above slow), -1 bearish, else
    a fractional score in between."""
    cols = list(ribbon.columns)
    score = sum(np.sign(ribbon[a] - ribbon[b]) for a, b in zip(cols[:-1], cols[1:]))
    return score / (len(cols) - 1)


def fibonacci_levels(low: float, high: float) -> dict[str, float]:
    """Retracement levels of an up-leg from ``low`` to ``high``."""
    span = high - low
    return {"0.0": high, "0.236": high - 0.236 * span, "0.382": high - 0.382 * span,
            "0.5": high - 0.5 * span, "0.618": high - 0.618 * span, "0.786": high - 0.786 * span, "1.0": low}


def volume_profile(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    rv = relative_volume(df["volume"], n)
    up_vol = df["volume"].where(df["close"] >= df["open"], 0.0).rolling(n, min_periods=n).sum()
    tot = df["volume"].rolling(n, min_periods=n).sum().replace(0.0, np.nan)
    return pd.DataFrame({"rel_volume": rv, "spike": rv >= 2.0, "buy_pressure": up_vol / tot,
                         "obv_slope": slope(obv(df), n)})
