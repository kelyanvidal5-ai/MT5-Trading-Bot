"""Cached indicator accessors shared by strategies (one computation per
MarketData object, however many strategies ask for it)."""
from __future__ import annotations

import pandas as pd

from ..indicators import candles, structure, ta
from ..market_data.timeframes import MarketData


def atr(d: MarketData, tf: str, n: int = 14) -> pd.Series:
    return d.cached((tf, "atr", n), lambda: ta.atr(d.frame(tf), n))


def ema(d: MarketData, tf: str, n: int) -> pd.Series:
    return d.cached((tf, "ema", n), lambda: ta.ema(d.frame(tf)["close"], n))


def rsi(d: MarketData, tf: str, n: int = 14) -> pd.Series:
    return d.cached((tf, "rsi", n), lambda: ta.rsi(d.frame(tf)["close"], n))


def adx(d: MarketData, tf: str, n: int = 14) -> pd.DataFrame:
    return d.cached((tf, "adx", n), lambda: ta.adx(d.frame(tf), n))


def macd(d: MarketData, tf: str) -> pd.DataFrame:
    return d.cached((tf, "macd"), lambda: ta.macd(d.frame(tf)["close"]))


def bollinger(d: MarketData, tf: str, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    return d.cached((tf, "bb", n, k), lambda: ta.bollinger(d.frame(tf)["close"], n, k))


def swings(d: MarketData, tf: str, left: int = 3, right: int = 3) -> pd.DataFrame:
    return d.cached((tf, "swings", left, right), lambda: structure.swings(d.frame(tf), left, right))


def mstructure(d: MarketData, tf: str, left: int = 3, right: int = 3) -> pd.DataFrame:
    return d.cached((tf, "ms", left, right),
                    lambda: structure.market_structure(d.frame(tf), swings(d, tf, left, right)))


def patterns(d: MarketData, tf: str) -> pd.DataFrame:
    return d.cached((tf, "candles"), lambda: candles.candle_patterns(d.frame(tf)))


def rejection(d: MarketData, tf: str) -> pd.DataFrame:
    return d.cached((tf, "rejection"), lambda: candles.rejection(d.frame(tf)))


def htf_trend(d: MarketData, htf: str, base_tf: str, fast: int = 20, slow: int = 50) -> pd.Series:
    """+1 / -1 / 0 trend of a higher timeframe (close vs fast EMA vs slow
    EMA), aligned to ``base_tf`` without look-ahead. 0 when the HTF history is
    too short - strategies treat 0 as "no confirmation"."""
    def compute() -> pd.Series:
        f = d.frame(htf)
        e_fast, e_slow = ema(d, htf, fast), ema(d, htf, slow)
        up = (f["close"] > e_fast) & (e_fast > e_slow)
        dn = (f["close"] < e_fast) & (e_fast < e_slow)
        return (up.astype(int) - dn.astype(int)).astype(float)
    raw = d.cached((htf, "trend", fast, slow), compute)
    return d.htf(raw, htf, base_tf).fillna(0.0)
