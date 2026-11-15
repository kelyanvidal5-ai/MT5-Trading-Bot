"""Smart Money Concepts: order-block / fair-value-gap retracement entries."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..core.models import ManagementPlan
from ..indicators import structure
from ..market_data.timeframes import MarketData
from . import common as c
from .base import Strategy, build, floor_risk


class SmcOrderBlock(Strategy):
    name = "smc_orderblock"
    style = "smart money concepts"
    hypothesis = ("After a break of structure in the direction of the H1/H4 trend, the first retracements "
                  "into the M15 order block (or fair value gap) that launched the break meet resting "
                  "institutional orders and continue in the trend direction.")
    entry_tf = "M15"
    context_tfs = ("H1", "H4")
    defaults = {"max_touches": 2, "sl_buffer_atr": 0.25, "min_sl_atr": 0.8, "rr": 2.5, "max_bars": 48,
                "min_rr": 1.5}
    grid = {"max_touches": [1, 2, 3], "rr": [2.0, 2.5, 3.0]}
    max_risk_atr = 3.5
    cooldown_bars = 6

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("M15")
        close, a = df["close"], c.atr(d, "M15")
        ms = c.mstructure(d, "M15")
        ob = d.cached(("M15", "ob"), lambda: structure.order_blocks(df, ms))
        fvg = d.cached(("M15", "fvg"), lambda: structure.fair_value_gaps(df))
        h1_struct = d.htf(c.mstructure(d, "H1")["trend"].astype(float), "H1", "M15").fillna(0.0)
        h4 = c.htf_trend(d, "H4", "M15")
        touches = int(self.p("max_touches"))
        # Zone = order block, else the fair value gap from the same impulse.
        bull_top = ob["bull_top"].fillna(fvg["bull_top"])
        bull_bot = ob["bull_bot"].fillna(fvg["bull_bot"])
        bull_age = ob["bull_age"].fillna(fvg["bull_age"])
        bull_touch = np.where(ob["bull_top"].notna(), ob["bull_touches"], fvg["bull_touches"])
        bear_top = ob["bear_top"].fillna(fvg["bear_top"])
        bear_bot = ob["bear_bot"].fillna(fvg["bear_bot"])
        bear_age = ob["bear_age"].fillna(fvg["bear_age"])
        bear_touch = np.where(ob["bear_bot"].notna(), ob["bear_touches"], fvg["bear_touches"])

        long = (h1_struct > 0) & (h4 >= 0) & (ms["trend"] == 1) & (bull_age >= 2) & (bull_touch <= touches) & \
               (df["low"] <= bull_top) & (close > bull_bot) & (close > df["open"]) & (close > close.shift(1))
        short = (h1_struct < 0) & (h4 <= 0) & (ms["trend"] == -1) & (bear_age >= 2) & (bear_touch <= touches) & \
                (df["high"] >= bear_bot) & (close < bear_top) & (close < df["open"]) & (close < close.shift(1))
        buf = self.p("sl_buffer_atr") * a
        sl_l = floor_risk(close, np.minimum(bull_bot, df["low"]) - buf, a, self.p("min_sl_atr"), True)
        sl_s = floor_risk(close, np.maximum(bear_top, df["high"]) + buf, a, self.p("min_sl_atr"), False)
        confluence = np.where(long, ob["bull_top"].notna() & fvg["bull_top"].notna(),
                              np.where(short, ob["bear_top"].notna() & fvg["bear_top"].notna(), False))
        strength = 0.45 + 0.25 * pd.Series(confluence, index=df.index).astype(float) + 0.3 * h4.abs()
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, max_bars=int(self.p("max_bars")), atr=atr)
