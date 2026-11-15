"""Trend-continuation strategies: scalping, intraday VWAP, EMA trend
following and MACD momentum."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..core.models import ManagementPlan
from ..indicators import ta
from ..market_data.timeframes import MarketData
from . import common as c
from .base import Strategy, build, floor_risk, unit


class ScalpPullback(Strategy):
    name = "scalp_pullback"
    style = "scalping"
    hypothesis = ("During the London/New York sessions, a shallow M5 pullback to the 20-EMA inside an "
                  "established M15 trend tends to resume in the trend direction before it reverses.")
    entry_tf = "M5"
    context_tfs = ("M15", "H1")
    sessions = ("london", "newyork")
    defaults = {"adx_min": 22.0, "rsi_reset": 45.0, "sl_buffer_atr": 0.3, "min_sl_atr": 1.0, "rr": 1.5,
                "max_bars": 24, "min_rr": 1.0}
    grid = {"adx_min": [18.0, 22.0, 26.0], "rr": [1.2, 1.5, 2.0]}
    cooldown_bars = 6

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("M5")
        close, a = df["close"], c.atr(d, "M5")
        e20, e50, r = c.ema(d, "M5", 20), c.ema(d, "M5", 50), c.rsi(d, "M5", 7)
        t15 = c.htf_trend(d, "M15", "M5")
        adx15 = d.htf(c.adx(d, "M15")["adx"], "M15", "M5")
        t60 = c.htf_trend(d, "H1", "M5")
        strong = adx15 >= self.p("adx_min")
        reset_lo, reset_hi = self.p("rsi_reset"), 100.0 - self.p("rsi_reset")
        long = (t15 > 0) & (t60 >= 0) & strong & (df["low"] <= e20) & (close > e20) & (close > df["open"]) & \
               (close > e50) & (r.shift(1) < reset_lo + 10) & (r > r.shift(1))
        short = (t15 < 0) & (t60 <= 0) & strong & (df["high"] >= e20) & (close < e20) & (close < df["open"]) & \
                (close < e50) & (r.shift(1) > reset_hi - 10) & (r < r.shift(1))
        buf = self.p("sl_buffer_atr") * a
        sl_l = floor_risk(close, df["low"].rolling(2).min() - buf, a, self.p("min_sl_atr"), True)
        sl_s = floor_risk(close, df["high"].rolling(2).max() + buf, a, self.p("min_sl_atr"), False)
        strength = 0.35 + 0.4 * unit(adx15, self.p("adx_min"), 45.0) + 0.25 * (t60.abs())
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, max_bars=int(self.p("max_bars")), atr=atr)


class IntradayVwap(Strategy):
    name = "intraday_vwap"
    style = "intraday"
    hypothesis = ("When the H1 trend is established, intraday pullbacks to the session VWAP are bought/sold "
                  "by participants benchmarked to VWAP, so price tends to leave VWAP in the trend direction.")
    entry_tf = "M15"
    context_tfs = ("H1",)
    sessions = ("london", "newyork")
    defaults = {"touch_atr": 0.15, "max_ext_atr": 1.2, "min_sl_atr": 1.0, "rr": 2.0, "max_bars": 32,
                "min_rr": 1.0}
    grid = {"rr": [1.5, 2.0, 2.5], "max_ext_atr": [0.8, 1.2, 1.6]}

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("M15")
        close, a = df["close"], c.atr(d, "M15")
        v = d.cached(("M15", "vwap"), lambda: ta.vwap(df))["vwap"]
        bias = c.htf_trend(d, "H1", "M15")
        adx_h1 = d.htf(c.adx(d, "H1")["adx"], "H1", "M15")
        touch, ext = self.p("touch_atr") * a, self.p("max_ext_atr") * a
        long = (bias > 0) & (df["low"] <= v + touch) & (close > v) & (close > df["open"]) & (close - v <= ext) & \
               (close.shift(1) >= v.shift(1) - ext)
        short = (bias < 0) & (df["high"] >= v - touch) & (close < v) & (close < df["open"]) & (v - close <= ext) & \
                (close.shift(1) <= v.shift(1) + ext)
        sl_l = floor_risk(close, np.minimum(df["low"], v) - 0.4 * a, a, self.p("min_sl_atr"), True)
        sl_s = floor_risk(close, np.maximum(df["high"], v) + 0.4 * a, a, self.p("min_sl_atr"), False)
        strength = 0.4 + 0.4 * unit(adx_h1, 18.0, 40.0) + 0.2 * unit(ext - (close - v).abs(), 0 * a, ext)
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, max_bars=int(self.p("max_bars")), atr=atr)


class TrendEma(Strategy):
    name = "trend_ema"
    style = "trend following"
    hypothesis = ("Trends confirmed on H1, H4 and D1 persist: buying pullbacks into the H1 20/50-EMA zone "
                  "while all three timeframes agree captures continuation with a favourable reward/risk.")
    entry_tf = "H1"
    context_tfs = ("H4", "D1")
    defaults = {"adx_min": 22.0, "zone_atr": 0.25, "sl_atr": 2.0, "rr": 3.0, "trail_atr": 2.5, "min_rr": 1.5}
    grid = {"adx_min": [18.0, 22.0, 26.0], "sl_atr": [1.5, 2.0, 2.5], "rr": [2.0, 3.0]}
    cooldown_bars = 6

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("H1")
        close, a = df["close"], c.atr(d, "H1")
        e20, e50, e200 = c.ema(d, "H1", 20), c.ema(d, "H1", 50), c.ema(d, "H1", 200)
        adx_ = c.adx(d, "H1")["adx"]
        h4 = c.htf_trend(d, "H4", "H1")
        d1 = c.htf_trend(d, "D1", "H1", fast=10, slow=20)
        zone = self.p("zone_atr") * a
        up = (e20 > e50) & (e50 > e200) & (h4 > 0) & (d1 >= 0) & (adx_ >= self.p("adx_min"))
        dn = (e20 < e50) & (e50 < e200) & (h4 < 0) & (d1 <= 0) & (adx_ >= self.p("adx_min"))
        long = up & (df["low"] <= e20 + zone) & (close > e20) & (close > df["open"]) & (df["low"] > e50 - zone)
        short = dn & (df["high"] >= e20 - zone) & (close < e20) & (close < df["open"]) & (df["high"] < e50 + zone)
        sl_l, sl_s = close - self.p("sl_atr") * a, close + self.p("sl_atr") * a
        strength = 0.35 + 0.35 * unit(adx_, self.p("adx_min"), 45.0) + 0.3 * d1.abs()
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, trail_atr_mult=float(self.p("trail_atr")), trail_start_r=1.5,
                              atr=atr)


class MacdMomentum(Strategy):
    name = "macd_momentum"
    style = "momentum"
    hypothesis = ("A fresh H1 MACD-histogram zero-cross in the direction of H4 momentum, with price on the "
                  "right side of the 50-EMA, marks an acceleration that persists for several hours.")
    entry_tf = "H1"
    context_tfs = ("H4",)
    defaults = {"roc_n": 10, "sl_atr": 1.5, "rr": 2.0, "max_bars": 48, "min_rr": 1.0}
    grid = {"sl_atr": [1.2, 1.5, 2.0], "rr": [1.5, 2.0, 2.5]}
    cooldown_bars = 8

    def _compute(self, d: MarketData) -> pd.DataFrame:
        df = d.frame("H1")
        close, a = df["close"], c.atr(d, "H1")
        m = c.macd(d, "H1")
        h4_hist = d.htf(c.macd(d, "H4")["hist"], "H4", "H1")
        e50 = c.ema(d, "H1", 50)
        rc = ta.roc(close, int(self.p("roc_n")))
        rc_rank = ta.rolling_percentile(rc.abs(), 200)
        cross_up = (m["hist"] > 0) & (m["hist"].shift(1) <= 0)
        cross_dn = (m["hist"] < 0) & (m["hist"].shift(1) >= 0)
        long = cross_up & (close > e50) & (rc > 0) & (h4_hist > 0) & (close > df["open"])
        short = cross_dn & (close < e50) & (rc < 0) & (h4_hist < 0) & (close < df["open"])
        sl_l, sl_s = close - self.p("sl_atr") * a, close + self.p("sl_atr") * a
        strength = 0.4 + 0.6 * rc_rank.fillna(0.5)
        return build(df.index, close, long, short, sl_l, sl_s, self.p("rr"), a, strength)

    def management(self, atr: float) -> ManagementPlan:
        return ManagementPlan(breakeven_at_r=1.0, max_bars=int(self.p("max_bars")), atr=atr)

