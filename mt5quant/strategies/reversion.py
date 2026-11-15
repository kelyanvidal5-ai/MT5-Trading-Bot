"""Counter-move strategies: Bollinger mean reversion and liquidity sweeps."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..core.models import ManagementPlan, Regime
from ..indicators import structure
from ..market_data.timeframes import MarketData
from . import common as c
from .base import Strategy, build, floor_risk, unit


class BollingerReversion(Strategy):
    name = "bb_reversion"
    style = "mean reversion"
    hypothesis = ("In a non-trending market (low H1 ADX), a close back inside the Bollinger Bands "
                  "after an oversold/overbought excursion reverts toward the 20-period mean.")
    entry_tf = "M15"
    context_tfs = ("H1",)
    regimes = frozenset({Regime.RANGE, Regime.QUIET})
    with_trend = False
    defaults = {"bb_k": 2.0, "rsi_extreme": 30.0, "adx_max": 22.0, "sl_buffer_atr": 0.5, "min_sl_atr": 0.8,
                "max_bars": 24, "min_rr": 1.0}
    grid = {"bb_k": [2.0, 2.3, 2.6], "rsi_extreme": [25.0, 30.0], "adx_max": [18.0, 22.0, 26.0]}

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("M15")
        close, a = df["close"], c.atr(d, "M15")
        bb = c.bollinger(d, "M15", 20, float(self.p("bb_k")))
        r = c.rsi(d, "M15", 14)
        adx15 = c.adx(d, "M15")["adx"]
        adx60 = d.htf(c.adx(d, "H1")["adx"], "H1", "M15")
        # The H1 regime must be non-trending; M15 ADX is allowed to be higher
        # because the excursion to the band itself lifts short-term ADX.
        ranging = (adx60 <= self.p("adx_max")) & (adx15 <= self.p("adx_max") + 12.0)
        lo, hi = self.p("rsi_extreme"), 100.0 - self.p("rsi_extreme")
        long = ranging & (close.shift(1) < bb["lower"].shift(1)) & (close > bb["lower"]) & \
               (r.shift(1) <= lo) & (close > df["open"])
        short = ranging & (close.shift(1) > bb["upper"].shift(1)) & (close < bb["upper"]) & \
                (r.shift(1) >= hi) & (close < df["open"])
        buf = self.p("sl_buffer_atr") * a
        sl_l = floor_risk(close, df["low"].rolling(3).min() - buf, a, self.p("min_sl_atr"), True)
        sl_s = floor_risk(close, df["high"].rolling(3).max() + buf, a, self.p("min_sl_atr"), False)
        depth = np.where(long, lo - r.shift(1), np.where(short, r.shift(1) - hi, 0.0))
        strength = 0.4 + 0.3 * unit(pd.Series(depth, index=df.index), 0.0, 12.0) + \
            0.3 * unit(self.p("adx_max") - adx60, 0.0, 10.0)
        # Target is the mean itself; the min_rr filter drops setups too close to it.
        return build(df.index, close, long, short, sl_l, sl_s, 1.0, a, strength, tp_long=bb["mid"], tp_short=bb["mid"])

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(max_bars=int(self.p("max_bars")), atr=atr)


class LiquiditySweep(Strategy):
    name = "liquidity_sweep"
    style = "liquidity sweep"
    hypothesis = ("Stops cluster beyond the previous day's extremes and obvious swing points. When price runs "
                  "those stops and closes back inside on the same M15 bar, the breakout has failed and price "
                  "tends to rotate away from the swept level.")
    entry_tf = "M15"
    context_tfs = ("H1",)
    regimes = frozenset({Regime.RANGE, Regime.QUIET, Regime.TREND_UP, Regime.TREND_DOWN})
    with_trend = False
    sessions = ("london", "newyork")
    defaults = {"min_pierce_atr": 0.1, "sl_buffer_atr": 0.3, "min_sl_atr": 0.8, "rr": 2.0, "adx_max": 35.0,
                "max_bars": 32, "min_rr": 1.2}
    grid = {"min_pierce_atr": [0.05, 0.1, 0.2], "rr": [1.5, 2.0, 2.5]}
    max_risk_atr = 3.0

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("M15")
        close, a = df["close"], c.atr(d, "M15")
        ls = d.cached(("M15", "sweeps", self.p("min_pierce_atr")),
                      lambda: structure.liquidity_sweeps(df, c.swings(d, "M15"),
                                                         min_pierce_atr=float(self.p("min_pierce_atr"))))
        rej = c.rejection(d, "M15")
        h1 = c.htf_trend(d, "H1", "M15")
        adx60 = d.htf(c.adx(d, "H1")["adx"], "H1", "M15")
        # Do not fade a sweep that is in the direction of a strong H1 trend.
        calm = adx60 <= self.p("adx_max")
        long = (ls["sweep"] == 1) & (rej["bull"] | (close > df["open"])) & (calm | (h1 >= 0))
        short = (ls["sweep"] == -1) & (rej["bear"] | (close < df["open"])) & (calm | (h1 <= 0))
        buf = self.p("sl_buffer_atr") * a
        sl_l = floor_risk(close, df["low"] - buf, a, self.p("min_sl_atr"), True)
        sl_s = floor_risk(close, df["high"] + buf, a, self.p("min_sl_atr"), False)
        wick = np.where(long, close - df["low"], np.where(short, df["high"] - close, 0.0))
        strength = 0.4 + 0.35 * unit(pd.Series(wick, index=df.index) / a, 0.3, 1.5) + \
            0.25 * (rej["bull"] | rej["bear"]).astype(float)
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, max_bars=int(self.p("max_bars")), atr=atr)
