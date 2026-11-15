"""Strategy framework.

A strategy is a *pure function of completed bars*: ``generate(MarketData)``
returns one row per entry-timeframe bar saying whether a trade should be
opened at the close of that bar, with its stop, target and a 0..1 strength.

The same ``generate`` code is used by the backtester (over all history at
once) and by the live engine (on a trailing window, reading the last row),
so what is tested is exactly what trades.

Rules every strategy must respect (checked by the test-suite):
* a clear, falsifiable hypothesis (``hypothesis``)
* strictly causal computations (no look-ahead)
* every signal carries a stop-loss and a take-profit on the correct side
* no signal sizing, no martingale, no averaging: sizing belongs to risk
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

import numpy as np
import pandas as pd

from ..core.models import ManagementPlan, Regime, Side, Signal
from ..market_data.sessions import MarketCalendar
from ..market_data.timeframes import MarketData, tf_delta

TREND = frozenset({Regime.TREND_UP, Regime.TREND_DOWN})


class Strategy(ABC):
    name: ClassVar[str]
    style: ClassVar[str]                       # e.g. "scalping", "trend following"
    hypothesis: ClassVar[str]
    entry_tf: ClassVar[str]
    context_tfs: ClassVar[tuple[str, ...]] = ()
    regimes: ClassVar[frozenset[Regime]] = TREND
    with_trend: ClassVar[bool] = True          # must agree with a directional regime
    sessions: ClassVar[Optional[tuple[str, ...]]] = None
    defaults: ClassVar[dict[str, Any]] = {}
    grid: ClassVar[dict[str, list]] = {}       # small, pre-declared optimisation grid
    warmup_bars: ClassVar[int] = 250
    cooldown_bars: ClassVar[int] = 4           # bars to wait after a signal
    min_risk_atr: ClassVar[float] = 0.6        # stop distance bounds, in ATR
    max_risk_atr: ClassVar[float] = 4.0

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params: dict[str, Any] = {**self.defaults, **params}

    def p(self, key: str) -> Any:
        return self.params[key]

    @property
    def timeframes(self) -> list[str]:
        return [self.entry_tf, *self.context_tfs]

    # ------------------------------------------------------------------ core
    @abstractmethod
    def _compute(self, data: MarketData) -> pd.DataFrame:
        """Return a frame indexed like the entry timeframe with columns
        ``side`` (+1/-1/0), ``sl``, ``tp``, ``strength`` (0..1), ``atr``."""

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(atr=atr)

    def generate(self, data: MarketData) -> pd.DataFrame:
        df = data.frame(self.entry_tf)
        raw = self._compute(data)
        close = df["close"].values
        side = np.nan_to_num(raw["side"].values.astype(float), nan=0.0).astype(np.int8)
        sl, tp = raw["sl"].values.astype(float), raw["tp"].values.astype(float)
        atr_ = raw["atr"].values.astype(float)
        strength = np.clip(np.nan_to_num(raw["strength"].values.astype(float), nan=0.0), 0.0, 1.0)

        risk = (close - sl) * side
        reward = (tp - close) * side
        with np.errstate(invalid="ignore", divide="ignore"):
            ok = (side != 0) & np.isfinite(sl) & np.isfinite(tp) & np.isfinite(atr_) & (atr_ > 0)
            ok &= (risk >= self.min_risk_atr * atr_) & (risk <= self.max_risk_atr * atr_)
            ok &= reward >= self.params.get("min_rr", 1.0) * risk
        ok[: self.warmup_bars] = False
        if self.sessions:
            # Evaluated at the bar close, which is when the order would be sent.
            ok &= MarketCalendar.session_mask(df.index + tf_delta(self.entry_tf), self.sessions)
        side = np.where(ok, side, 0).astype(np.int8)
        # Cool-down: suppress signals for a few bars after one fires.
        if self.cooldown_bars > 0:
            last = -10 ** 9
            for i in np.flatnonzero(side):
                if i - last <= self.cooldown_bars:
                    side[i] = 0
                else:
                    last = i
        out = pd.DataFrame({"side": side, "sl": np.where(side != 0, sl, np.nan),
                            "tp": np.where(side != 0, tp, np.nan), "strength": np.where(side != 0, strength, 0.0),
                            "atr": atr_, "close": close}, index=df.index)
        return out

    # ---------------------------------------------------------------- output
    def to_signal(self, symbol: str, bar_open: pd.Timestamp, row: Any, digits: int = 5) -> Signal:
        side = Side.BUY if int(row.side) > 0 else Side.SELL
        return Signal(
            strategy=self.name, symbol=symbol, timeframe=self.entry_tf,
            time=(bar_open + tf_delta(self.entry_tf)).to_pydatetime(), side=side,
            sl=round(float(row.sl), digits), tp=round(float(row.tp), digits), ref_price=float(row.close),
            strength=float(row.strength), management=self.management(float(row.atr)),
            reasons=[self.hypothesis], meta={"atr": float(row.atr), "style": self.style})

    def latest_signal(self, data: MarketData) -> Optional[Signal]:
        """Signal produced by the most recent completed entry-timeframe bar."""
        df = data.frame(self.entry_tf)
        if len(df) <= self.warmup_bars:
            return None
        sig = self.generate(data)
        row = sig.iloc[-1]
        if int(row.side) == 0:
            return None
        return self.to_signal(data.symbol, sig.index[-1], row, data.spec.digits)

    def describe(self) -> dict:
        return {"name": self.name, "style": self.style, "hypothesis": self.hypothesis,
                "entry_tf": self.entry_tf, "context_tfs": list(self.context_tfs),
                "regimes": sorted(r.value for r in self.regimes), "params": dict(self.params),
                "sessions": list(self.sessions or [])}


# ------------------------------------------------------------------- helpers
def blank(index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame({"side": 0, "sl": np.nan, "tp": np.nan, "strength": 0.0, "atr": np.nan}, index=index)


def build(index: pd.Index, close: pd.Series, long: pd.Series, short: pd.Series, sl_long: pd.Series,
          sl_short: pd.Series, rr: float, atr_: pd.Series, strength: pd.Series | float,
          tp_long: Optional[pd.Series] = None, tp_short: Optional[pd.Series] = None) -> pd.DataFrame:
    """Assemble a signal frame from boolean entry conditions."""
    long = long.fillna(False).astype(bool) & ~short.fillna(False).astype(bool)
    short = short.fillna(False).astype(bool) & ~long
    side = np.where(long, 1, np.where(short, -1, 0))
    sl = np.where(long, sl_long, np.where(short, sl_short, np.nan))
    tpl = tp_long if tp_long is not None else close + rr * (close - sl_long)
    tps = tp_short if tp_short is not None else close - rr * (sl_short - close)
    tp = np.where(long, tpl, np.where(short, tps, np.nan))
    return pd.DataFrame({"side": side, "sl": sl, "tp": tp, "strength": strength, "atr": atr_}, index=index)


def floor_risk(close: pd.Series, stop: pd.Series, atr_: pd.Series, min_atr: float, long: bool) -> pd.Series:
    """Push a structural stop out to at least ``min_atr`` ATR from the close."""
    if long:
        return close - np.maximum(close - stop, min_atr * atr_)
    return close + np.maximum(stop - close, min_atr * atr_)


def unit(x: pd.Series, lo: float, hi: float) -> pd.Series:
    """Linear map of ``x`` from [lo, hi] to [0, 1], clipped."""
    return ((x - lo) / (hi - lo)).clip(0.0, 1.0)


def consecutive(flag: pd.Series) -> pd.Series:
    """Length of the current run of True values."""
    f = flag.fillna(False).astype(bool)
    grp = (~f).cumsum()
    return f.astype(int).groupby(grp).cumsum()
