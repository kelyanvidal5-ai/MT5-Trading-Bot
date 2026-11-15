"""Breakout and volatility-expansion strategies."""
from __future__ import annotations

import pandas as pd

from ..core.models import ManagementPlan, Regime
from ..indicators import ta
from ..market_data.timeframes import MarketData
from . import common as c
from .base import Strategy, build, consecutive, unit


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    style = "breakout"
    hypothesis = ("A close outside the prior 20-bar H1 range that follows a volatility contraction and comes "
                  "with above-average volume reflects new information, and the move tends to extend.")
    entry_tf = "H1"
    context_tfs = ("H4",)
    regimes = frozenset({Regime.QUIET, Regime.RANGE, Regime.TREND_UP, Regime.TREND_DOWN})
    defaults = {"channel": 20, "squeeze_pct": 0.45, "min_rel_volume": 1.1, "sl_atr": 1.5, "rr": 2.5,
                "trail_atr": 2.5, "max_bars": 72, "min_rr": 1.2}
    grid = {"channel": [20, 30, 40], "squeeze_pct": [0.35, 0.45, 0.6], "rr": [2.0, 2.5]}
    cooldown_bars = 8

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("H1")
        close, a = df["close"], c.atr(d, "H1")
        n = int(self.p("channel"))
        ch = d.cached(("H1", "donchian", n), lambda: ta.donchian(df, n))
        width_rank = d.cached(("H1", "bbw_rank"), lambda: ta.rolling_percentile(c.bollinger(d, "H1")["width"], 120))
        contracted = width_rank.shift(1) <= self.p("squeeze_pct")
        rel_vol = d.cached(("H1", "relvol"), lambda: ta.relative_volume(df["volume"], 20))
        active = rel_vol >= self.p("min_rel_volume")
        rng = (df["high"] - df["low"]).replace(0.0, float("nan"))
        pos = (close - df["low"]) / rng
        h4 = c.htf_trend(d, "H4", "H1")
        long = (close > ch["upper"]) & (close.shift(1) <= ch["upper"].shift(1)) & contracted & active & \
               (pos >= 0.65) & (h4 >= 0)
        short = (close < ch["lower"]) & (close.shift(1) >= ch["lower"].shift(1)) & contracted & active & \
                (pos <= 0.35) & (h4 <= 0)
        sl_l, sl_s = close - self.p("sl_atr") * a, close + self.p("sl_atr") * a
        strength = 0.35 + 0.3 * unit(rel_vol, 1.0, 2.5) + 0.2 * (1.0 - width_rank.shift(1).fillna(0.5)) + \
            0.15 * h4.abs()
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, trail_atr_mult=float(self.p("trail_atr")), trail_start_r=1.0,
                              max_bars=int(self.p("max_bars")), atr=atr)


class VolSqueeze(Strategy):
    name = "vol_squeeze"
    style = "volatility"
    hypothesis = ("Volatility is mean-reverting: after Bollinger Bands sit inside Keltner Channels for several "
                  "H1 bars, the release of the squeeze starts an expansion in the direction of momentum.")
    entry_tf = "H1"
    context_tfs = ("H4",)
    regimes = frozenset({Regime.QUIET, Regime.RANGE, Regime.TREND_UP, Regime.TREND_DOWN})
    with_trend = False
    defaults = {"min_squeeze_bars": 4, "kc_mult": 1.5, "sl_atr": 1.5, "rr": 2.0, "trail_atr": 2.0,
                "max_bars": 48, "min_rr": 1.0}
    grid = {"min_squeeze_bars": [3, 4, 6], "kc_mult": [1.25, 1.5, 2.0]}
    cooldown_bars = 8

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("H1")
        close, a = df["close"], c.atr(d, "H1")
        bb = c.bollinger(d, "H1")
        kc = d.cached(("H1", "kc", self.p("kc_mult")), lambda: ta.keltner(df, 20, float(self.p("kc_mult"))))
        on = (bb["upper"] < kc["upper"]) & (bb["lower"] > kc["lower"])
        run = consecutive(on)
        released = (~on) & (run.shift(1) >= int(self.p("min_squeeze_bars")))
        dc = d.cached(("H1", "donchian_incl", 20),
                      lambda: (df["high"].rolling(20).max() + df["low"].rolling(20).min()) / 2.0)
        mom = close - (dc + bb["mid"]) / 2.0
        h4 = c.htf_trend(d, "H4", "H1")
        long = released & (mom > 0) & (mom > mom.shift(1)) & (h4 >= 0) & (close > df["open"])
        short = released & (mom < 0) & (mom < mom.shift(1)) & (h4 <= 0) & (close < df["open"])
        sl_l, sl_s = close - self.p("sl_atr") * a, close + self.p("sl_atr") * a
        strength = 0.4 + 0.35 * unit(run.shift(1), self.p("min_squeeze_bars"), 24) + 0.25 * unit(mom.abs() / a, 0.2, 1.5)
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, trail_atr_mult=float(self.p("trail_atr")), trail_start_r=1.0,
                              max_bars=int(self.p("max_bars")), atr=atr)
