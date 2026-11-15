"""Timeframe utilities: resampling and look-ahead-safe multi-timeframe alignment.

Convention (same as MetaTrader): a bar is indexed by its OPEN time in UTC and
becomes known only at ``open + timeframe``. Every helper here respects that,
which is what makes multi-timeframe features safe from look-ahead bias.
"""
from __future__ import annotations

from typing import Mapping, Optional

import numpy as np
import pandas as pd

from ..core.models import SymbolSpec, Timeframe

OHLC_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

ALL_TIMEFRAMES = [tf.value for tf in Timeframe]


def tf_delta(tf: str) -> pd.Timedelta:
    return pd.Timedelta(minutes=Timeframe(tf).minutes)


def validate_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise and sanity-check an OHLCV frame. Raises on unusable data."""
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"bars missing columns: {sorted(missing)}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("bars must be indexed by a DatetimeIndex (bar open time)")
    out = df.copy()
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if "volume" not in out.columns:
        out["volume"] = out["tick_volume"] if "tick_volume" in out.columns else 0.0
    cols = ["open", "high", "low", "close", "volume"] + (["spread"] if "spread" in out.columns else [])
    out = out[cols].astype(float)
    if out[["open", "high", "low", "close"]].isna().any().any():
        raise ValueError("bars contain NaN prices")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("bars contain non-positive prices")
    bad = (out["high"] < out[["open", "close", "low"]].max(axis=1) - 1e-12) | \
          (out["low"] > out[["open", "close", "high"]].min(axis=1) + 1e-12)
    if bad.any():
        raise ValueError(f"{int(bad.sum())} bars violate high/low consistency (first: {out.index[bad][0]})")
    return out


def _trading_period_open(index: pd.DatetimeIndex, weekly: bool) -> pd.DatetimeIndex:
    """UTC open time of the FX trading day (or week) each bar belongs to.
    A trading day runs 17:00 -> 17:00 New York, DST-aware."""
    ny = index.tz_convert("America/New_York")
    day = (ny + pd.Timedelta(hours=7)).normalize().tz_localize(None)     # naive trading date
    if weekly:
        day = day - pd.to_timedelta(day.weekday, unit="D")               # Monday trading day
    opened = (day - pd.Timedelta(hours=7)).tz_localize("America/New_York", ambiguous="NaT",
                                                       nonexistent="shift_forward")
    return opened.tz_convert("UTC")


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate bars to a higher timeframe. The last output bar may be
    incomplete; :func:`align_htf` never exposes an incomplete bar.

    Daily and weekly candles follow the New York 17:00 close, which gives five
    daily candles per week (the convention of most MT5 FX brokers)."""
    agg = {k: v for k, v in OHLC_AGG.items() if k in df.columns}
    if "spread" in df.columns:
        agg["spread"] = "mean"
    t = Timeframe(tf)
    if t.minutes >= 1440:
        label = _trading_period_open(df.index, weekly=t.minutes > 1440)
        out = df.groupby(label).agg(agg)
        out.index = pd.DatetimeIndex(out.index, tz="UTC")
        out.index.name = df.index.name
        return out
    return df.resample(t.pandas_rule, label="left", closed="left").agg(agg).dropna(subset=["open"])


def align_htf(base_index: pd.DatetimeIndex, base_tf: str, htf: pd.DataFrame | pd.Series,
              htf_tf: str) -> pd.DataFrame | pd.Series:
    """Project higher-timeframe values onto base-timeframe bars without
    look-ahead: the row for a base bar holds the most recent HTF bar whose
    CLOSE time is <= that base bar's close time (NaN before the first one)."""
    htf_close = (htf.index + tf_delta(htf_tf)).values
    base_close = (base_index + tf_delta(base_tf)).values
    pos = np.searchsorted(htf_close, base_close, side="right") - 1
    valid = pos >= 0
    take = np.where(valid, pos, 0)
    if isinstance(htf, pd.Series):
        vals = htf.values[take].astype(float)
        vals[~valid] = np.nan
        return pd.Series(vals, index=base_index, name=htf.name)
    vals = htf.values[take].astype(float)
    vals[~valid] = np.nan
    return pd.DataFrame(vals, index=base_index, columns=htf.columns)


def drop_incomplete(df: pd.DataFrame, tf: str, now: pd.Timestamp) -> pd.DataFrame:
    """Remove the still-forming last bar from a live frame."""
    if df.empty:
        return df
    closes = df.index + tf_delta(tf)
    return df[closes <= now]


class MarketData:
    """All timeframes for one symbol plus a per-run indicator cache.

    Strategies receive this object and may only read from it. HTF values are
    always obtained through :meth:`htf`, which applies :func:`align_htf`.
    """

    def __init__(self, symbol: str, spec: SymbolSpec, frames: Mapping[str, pd.DataFrame]) -> None:
        self.symbol = symbol
        self.spec = spec
        self.frames: dict[str, pd.DataFrame] = dict(frames)
        self._cache: dict[tuple, object] = {}

    @classmethod
    def from_base(cls, symbol: str, spec: SymbolSpec, base: pd.DataFrame, base_tf: str,
                  timeframes: Optional[list[str]] = None) -> "MarketData":
        base = validate_bars(base)
        frames = {base_tf: base}
        base_min = Timeframe(base_tf).minutes
        for tf in timeframes or ALL_TIMEFRAMES:
            if Timeframe(tf).minutes > base_min:
                frames[tf] = resample(base, tf)
        return cls(symbol, spec, frames)

    def has(self, tf: str) -> bool:
        return tf in self.frames and len(self.frames[tf]) > 0

    def frame(self, tf: str) -> pd.DataFrame:
        if tf not in self.frames:
            raise KeyError(f"{self.symbol}: timeframe {tf} not loaded (have {sorted(self.frames)})")
        return self.frames[tf]

    def cached(self, key: tuple, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def htf(self, values: pd.Series | pd.DataFrame, htf_tf: str, base_tf: str):
        """Align a series computed on ``htf_tf`` to the ``base_tf`` index."""
        return align_htf(self.frame(base_tf).index, base_tf, values, htf_tf)

    def slice(self, end: pd.Timestamp) -> "MarketData":
        """Copy containing only bars that OPENED before ``end`` (for tests)."""
        return MarketData(self.symbol, self.spec, {tf: f[f.index < end] for tf, f in self.frames.items()})
