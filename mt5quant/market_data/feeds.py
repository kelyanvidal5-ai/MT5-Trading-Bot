"""Market data sources that do not need a terminal.

``SyntheticFeed``  random-walk prices advancing with a (possibly accelerated)
                   virtual clock. For demonstrations and tests ONLY - these
                   are not market prices and are labelled as such everywhere.
``ReplayFeed``     replays a historical bar file as if it were live; used by
                   integration tests and for dry-running the live loop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from ..core.models import SymbolSpec, Tick, Timeframe, utcnow
from ..execution.broker import BrokerError, MarketDataSource
from .specs import generic_spec
from .synthetic import generate_bars
from .timeframes import resample


def _stamp(t) -> pd.Timestamp:
    """UTC timestamp at whole-second precision in nanosecond resolution."""
    ts = pd.Timestamp(t)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.floor("s").as_unit("ns")


def _ns(df: pd.DataFrame) -> pd.DataFrame:
    if getattr(df.index, "unit", "ns") == "ns":
        return df
    out = df.copy()
    out.index = df.index.as_unit("ns")
    out.attrs = dict(df.attrs)
    return out


class ReplayFeed(MarketDataSource):
    """Serves bars from in-memory history up to a controllable clock."""

    generic_specs = True

    def __init__(self, bars: dict[str, pd.DataFrame], base_tf: str = "M5", account_currency: str = "USD",
                 spread_points: Optional[dict[str, float]] = None, default_spread_points: float = 10.0,
                 specs: Optional[dict[str, SymbolSpec]] = None, start: Optional[datetime] = None) -> None:
        self.base_tf = base_tf
        self.min_timeframe = base_tf
        # One timestamp resolution everywhere: mixed pandas units cannot be
        # compared losslessly by searchsorted.
        bars = {s: _ns(df) for s, df in bars.items()}
        self._bars = bars
        self._step = pd.Timedelta(minutes=Timeframe(base_tf).minutes)
        self._currency = account_currency
        self._spread = spread_points or {}
        self._default_spread = default_spread_points
        self._specs = dict(specs or {})
        self._cache: dict[tuple, pd.DataFrame] = {}
        first_end = min(df.index[-1] for df in bars.values())
        self._now = _stamp(start) if start is not None else first_end + self._step
        self.connected = True
        self.synthetic = any(df.attrs.get("synthetic") for df in bars.values())

    # ---- clock
    def now(self) -> datetime:
        return self._now.to_pydatetime()

    def set_time(self, t: datetime | pd.Timestamp) -> None:
        self._now = _stamp(t)

    def advance(self, bars: int = 1) -> None:
        self._now = self._now + self._step * bars

    # ---- MarketDataSource
    def is_connected(self) -> bool:
        return self.connected

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._bars:
            raise BrokerError(f"unknown symbol {symbol}")
        if symbol not in self._specs:
            self._specs[symbol] = generic_spec(symbol, self._currency)
        return self._specs[symbol]

    def _visible(self, symbol: str) -> pd.DataFrame:
        """Base bars whose OPEN time is <= now (the last one may be forming)."""
        if not self.connected:
            raise BrokerError("feed disconnected")
        if symbol not in self._bars:
            raise BrokerError(f"unknown symbol {symbol}")
        df = self._bars[symbol]
        pos = df.index.searchsorted(self._now, side="right")
        return df.iloc[:pos]

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if Timeframe(timeframe).minutes < Timeframe(self.base_tf).minutes:
            raise BrokerError(f"timeframe {timeframe} is finer than the feed's base {self.base_tf}")
        vis = self._visible(symbol)
        # Bars that are still forming at `now` must not expose their future
        # high/low/close: only fully completed base bars are aggregated, and
        # only fully completed higher-timeframe bars are returned.
        complete = vis[vis.index + self._step <= self._now]
        if timeframe == self.base_tf:
            return complete.iloc[-count:]
        step = pd.Timedelta(minutes=Timeframe(timeframe).minutes)
        key = (symbol, timeframe, self._now.floor(step) if step <= pd.Timedelta(hours=4) else len(complete) // 12)
        if key not in self._cache:
            if len(self._cache) > 256:
                self._cache.clear()
            frame = resample(complete, timeframe)
            self._cache[key] = frame[frame.index + step <= self._now]
        return self._cache[key].iloc[-count:]

    def tick(self, symbol: str) -> Tick:
        vis = self._visible(symbol)
        if vis.empty:
            raise BrokerError(f"no data yet for {symbol}")
        last = vis.iloc[-1]
        complete = vis.index[-1] + self._step <= self._now
        # A forming bar exposes only its open; a completed one its close.
        bid = float(last["close"] if complete else last["open"])
        when = (vis.index[-1] + self._step) if complete else vis.index[-1]
        spec = self.symbol_spec(symbol)
        pts = float(last["spread"]) if "spread" in vis.columns else self._spread.get(symbol, self._default_spread)
        return Tick(symbol, when.to_pydatetime(), bid, round(bid + pts * spec.point, spec.digits))


class SyntheticFeed(ReplayFeed):
    """Pre-generates a long synthetic path and reveals it in (scaled) real time."""

    def __init__(self, symbols: list[str], seed: int = 7, speed: float = 1.0, base_tf: str = "M5",
                 history_bars: int = 110_000, future_bars: int = 60_000, account_currency: str = "USD",
                 spread_points: Optional[dict[str, float]] = None) -> None:
        step = pd.Timedelta(minutes=Timeframe(base_tf).minutes)
        self._wall0 = utcnow()
        anchor = pd.Timestamp(self._wall0).floor(step)
        total = history_bars + future_bars
        start = anchor - step * int(history_bars * 7 / 5 + 2000)
        bars = {}
        for s in symbols:
            df = generate_bars(s, periods=total, tf=base_tf, seed=seed, start=start,
                               spread_points=(spread_points or {}).get(s, 10.0))
            bars[s] = df
        # Virtual time starts where `history_bars` of history are available.
        first = next(iter(bars.values()))
        self._virtual0 = first.index[min(history_bars, len(first) - 1)]
        self.speed = speed
        super().__init__(bars, base_tf=base_tf, account_currency=account_currency, spread_points=spread_points,
                         start=self._virtual0)
        self.synthetic = True

    def now(self) -> datetime:
        elapsed = (utcnow() - self._wall0).total_seconds() * self.speed
        self._now = _stamp(self._virtual0 + pd.Timedelta(seconds=int(elapsed)))
        return self._now.to_pydatetime()

    def _visible(self, symbol: str) -> pd.DataFrame:
        self.now()
        return super()._visible(symbol)
