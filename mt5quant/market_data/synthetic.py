"""Synthetic price generator.

*** These are NOT market prices. *** The generator exists to exercise the
software (tests, demos, pipeline dry-runs) when no broker history is
available. Results obtained on synthetic data say nothing about real-world
profitability and every report produced from it is labelled accordingly.

The process is a regime-switching random walk with volatility clustering,
intraday seasonality, occasional jumps and FX-style weekend gaps - enough
texture to make trend, range and volatility logic fire, with zero built-in
exploitable edge beyond regime persistence.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..core.models import Timeframe
from .sessions import MarketCalendar

_START_PRICE = {"EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": 150.00, "AUDUSD": 0.6600, "USDCHF": 0.8800,
                "USDCAD": 1.3600, "NZDUSD": 0.6100, "EURJPY": 162.00, "GBPJPY": 190.00, "EURGBP": 0.8550,
                "XAUUSD": 2300.0}
_ANNUAL_VOL = {"EURUSD": 0.075, "GBPUSD": 0.09, "USDJPY": 0.10, "XAUUSD": 0.16}


def fx_bar_index(start: pd.Timestamp, periods: int, tf: str) -> pd.DatetimeIndex:
    """``periods`` bar-open timestamps starting at ``start``, FX week only."""
    cal = MarketCalendar(0, 0, 0)
    step = pd.Timedelta(minutes=Timeframe(tf).minutes)
    out: list[pd.DatetimeIndex] = []
    count, cursor = 0, pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else \
        pd.Timestamp(start, tz="UTC")
    while count < periods:
        chunk = pd.date_range(cursor, periods=max(periods - count, 1000) * 2, freq=step)
        ny = chunk.tz_convert("America/New_York")
        wd, mins = ny.weekday.values, ny.hour.values * 60 + ny.minute.values
        keep = ~((wd == 5) | ((wd == 6) & (mins < 1020)) | ((wd == 4) & (mins >= 1020)))
        kept = chunk[keep][: periods - count]
        out.append(kept)
        count += len(kept)
        cursor = chunk[-1] + step
    del cal
    idx = out[0]
    for more in out[1:]:
        idx = idx.append(more)
    return idx


def generate_bars(symbol: str = "EURUSD", periods: int = 50_000, tf: str = "M5", seed: int = 7,
                  start: Optional[str | pd.Timestamp] = "2022-01-03 00:00", start_price: Optional[float] = None,
                  spread_points: float = 8.0, digits: Optional[int] = None,
                  drift_strength: float = 1.0) -> pd.DataFrame:
    """Generate OHLCV(+spread) bars. Deterministic for a given seed."""
    rng = np.random.default_rng(seed + (abs(hash(symbol)) % 1000 if False else sum(map(ord, symbol))))
    index = fx_bar_index(pd.Timestamp(start), periods, tf)
    n, sub = len(index), 6
    minutes = Timeframe(tf).minutes
    price0 = start_price or _START_PRICE.get(symbol, 1.0)
    if digits is None:
        digits = 3 if price0 > 20 else 5
        if price0 > 500:
            digits = 2
    ann_vol = _ANNUAL_VOL.get(symbol, 0.085)
    bars_per_year = 252 * 1440 / minutes
    base_sigma = ann_vol / np.sqrt(bars_per_year)

    # --- regime path: 0 range, 1 up, 2 down ; sticky Markov chain ------------
    stay = 1.0 - minutes / (60.0 * 36.0)          # average regime length ~36 hours
    stay = float(np.clip(stay, 0.5, 0.9995))
    regime = np.empty(n, dtype=np.int8)
    switch = rng.random(n) > stay
    choice = rng.choice(3, size=n, p=[0.5, 0.25, 0.25])
    cur = 0
    for i in range(n):
        if switch[i]:
            cur = choice[i]
        regime[i] = cur
    drift = np.where(regime == 1, 1.0, np.where(regime == 2, -1.0, 0.0)) * base_sigma * 0.12 * drift_strength

    # --- volatility: clustering (log-AR(1)) x intraday seasonality -----------
    logv = np.empty(n)
    shocks = rng.normal(0, 0.06, n)
    lv = 0.0
    for i in range(n):
        lv = 0.985 * lv + shocks[i]
        logv[i] = lv
    hour = index.hour.values + index.minute.values / 60.0
    season = 0.55 + 0.55 * np.exp(-((hour - 8.5) / 2.5) ** 2) + 0.75 * np.exp(-((hour - 14.0) / 2.5) ** 2)
    sigma = base_sigma * np.exp(logv) * season

    # --- sub-bar path -> OHLC ----------------------------------------------
    z = rng.standard_t(df=5, size=(n, sub)) / np.sqrt(5 / 3)
    steps = z * (sigma[:, None] / np.sqrt(sub)) + drift[:, None] / sub
    # range regime: mild mean reversion toward a slow anchor
    jumps = (rng.random(n) < (minutes / (1440.0 * 6.0))) * rng.normal(0, base_sigma * 9, n)
    steps[:, 0] += jumps
    # weekend gaps
    gap = np.zeros(n)
    dt = np.diff(index.values).astype("timedelta64[m]").astype(int)
    weekend = np.flatnonzero(dt > minutes * 3) + 1
    gap[weekend] = rng.normal(0, base_sigma * 6, len(weekend))
    steps[:, 0] += gap

    logp = np.log(price0) + np.cumsum(steps.ravel()).reshape(n, sub)
    # anchor mean reversion in range regimes keeps prices from drifting unrealistically
    close = np.exp(logp[:, -1])
    prev_close = np.concatenate(([price0], close[:-1]))
    open_ = prev_close * np.exp(gap)
    path = np.exp(logp)
    high = np.maximum(path.max(axis=1), np.maximum(open_, close))
    low = np.minimum(path.min(axis=1), np.minimum(open_, close))

    volume = np.maximum(1, (rng.gamma(4.0, 25.0, n) * (sigma / base_sigma) * minutes / 5.0)).round()
    pt = 10.0 ** -digits
    roll = np.abs((index.tz_convert("America/New_York").hour.values * 60 +
                   index.tz_convert("America/New_York").minute.values) - 1020) <= 45
    spread = spread_points * (1.0 + 0.6 * (season < 0.7)) * np.where(roll, 4.0, 1.0) * \
        np.clip(np.exp(logv), 0.8, 3.0)

    df = pd.DataFrame({
        "open": np.round(open_, digits), "high": np.round(high, digits), "low": np.round(low, digits),
        "close": np.round(close, digits), "volume": volume, "spread": np.round(spread, 1),
    }, index=index)
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    df.index.name = "time"
    df.attrs["synthetic"] = True
    df.attrs["point"] = pt
    return df
