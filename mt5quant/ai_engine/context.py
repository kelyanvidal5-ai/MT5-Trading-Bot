"""Multi-timeframe market context and the confidence model.

``build_context`` produces, for every entry-timeframe bar, what the engine
knew at that bar's close: the H1 regime, the trend of each higher timeframe
and the weighted primary trend. ``score`` turns a strategy's raw signals plus
that context into a 0..1 confidence with named components.

Both functions are vectorised and causal, and both the backtester and the
live engine call the same code - the live engine simply reads the last row.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..core.models import Regime, Timeframe
from ..indicators import ta
from ..market_data.timeframes import MarketData
from ..strategies.base import Strategy
from . import regime as regime_mod

TREND_TFS = ("M15", "H1", "H4", "D1", "W1")
# Weight of each timeframe in the primary trend (higher timeframes dominate).
TREND_WEIGHTS = {"M15": 0.05, "H1": 0.20, "H4": 0.30, "D1": 0.30, "W1": 0.15}
REGIME_TF = "H1"

WEIGHTS = {"strength": 0.30, "regime": 0.25, "timeframes": 0.25, "cost": 0.10, "news": 0.10}
META_BLEND = 0.30          # weight of a validated ML model in the final confidence
META_VETO = 0.40           # validated model probability below this vetoes the trade


def _tf_trend(d: MarketData, tf: str) -> pd.Series:
    """+1/-1/0 from price vs EMA20 vs EMA50 on ``tf`` (EMA10/20 on D1/W1
    where history is short)."""
    def compute() -> pd.Series:
        f = d.frame(tf)
        fast, slow = (10, 20) if Timeframe(tf).minutes >= 1440 else (20, 50)
        ef, es = ta.ema(f["close"], fast), ta.ema(f["close"], slow)
        up = (f["close"] > ef) & (ef > es)
        dn = (f["close"] < ef) & (ef < es)
        return (up.astype(int) - dn.astype(int)).astype(float)
    return d.cached((tf, "ctx_trend"), compute)


def build_context(d: MarketData, entry_tf: str) -> pd.DataFrame:
    """Context aligned to ``entry_tf`` bars (known at each bar's close)."""
    def compute() -> pd.DataFrame:
        base = d.frame(entry_tf)
        out = pd.DataFrame(index=base.index)
        if d.has(REGIME_TF) and Timeframe(entry_tf).minutes <= Timeframe(REGIME_TF).minutes:
            reg = d.cached((REGIME_TF, "regime"), lambda: regime_mod.classify(d.frame(REGIME_TF)))
            num = d.htf(reg[["direction", "adx", "efficiency", "vol_rank", "shock_z"]], REGIME_TF, entry_tf)
            codes = pd.Series(pd.Categorical(reg["regime"]).codes, index=reg.index, dtype=float)
            cats = list(pd.Categorical(reg["regime"]).categories)
            code_al = d.htf(codes, REGIME_TF, entry_tf)
            out["regime"] = [cats[int(c)] if c == c else Regime.UNKNOWN.value for c in code_al.values]
            out[["regime_dir", "adx_h1", "er_h1", "vol_rank", "shock_z"]] = num.values
        else:
            out["regime"] = Regime.UNKNOWN.value
            out[["regime_dir", "adx_h1", "er_h1", "vol_rank", "shock_z"]] = np.nan
        entry_min = Timeframe(entry_tf).minutes
        num_w = pd.Series(0.0, index=base.index)
        den_w = 0.0
        for tf in TREND_TFS:
            if not d.has(tf) or Timeframe(tf).minutes < entry_min:
                out[f"trend_{tf}"] = 0.0
                continue
            t = _tf_trend(d, tf)
            al = t.reindex(base.index).fillna(0.0) if tf == entry_tf else d.htf(t, tf, entry_tf).fillna(0.0)
            out[f"trend_{tf}"] = al.values
            if Timeframe(tf).minutes > entry_min:
                num_w = num_w + TREND_WEIGHTS[tf] * al.values
                den_w += TREND_WEIGHTS[tf]
        out["primary_trend"] = num_w / den_w if den_w else 0.0
        return out
    return d.cached((entry_tf, "context"), compute)


def regime_allows(strategy: Strategy, regime: np.ndarray, regime_dir: np.ndarray, side: np.ndarray) -> np.ndarray:
    """Strategy selector rule: is this strategy appropriate for the regime?"""
    allowed = {r.value for r in strategy.regimes}
    ok = np.isin(regime, list(allowed))
    if strategy.with_trend:
        # In a directional regime a trend strategy may only trade with it.
        ok &= (regime_dir == 0) | (np.sign(regime_dir) == np.sign(side))
    return ok


def score(strategy: Strategy, signals: pd.DataFrame, ctx: pd.DataFrame,
          spread_atr_ratio: Optional[np.ndarray | float] = None,
          news_factor: Optional[np.ndarray | float] = None,
          meta_prob: Optional[np.ndarray] = None) -> pd.DataFrame:
    """Confidence components for every bar of ``signals`` (0 where no signal).

    Returns columns: confidence, c_strength, c_regime, c_timeframes, c_cost,
    c_news, regime_ok, htf_conflict, meta_veto.
    """
    side = signals["side"].values.astype(float)
    active = side != 0
    regime = ctx["regime"].values
    rdir = np.nan_to_num(ctx["regime_dir"].values.astype(float))
    reg_ok = regime_allows(strategy, regime, rdir, side)

    c_strength = signals["strength"].values.astype(float)
    # Regime fit: full marks when a directional regime agrees with the trade.
    agree = (np.sign(rdir) == np.sign(side)) & (rdir != 0)
    c_regime = np.where(reg_ok, np.where(agree | ~strategy.with_trend, 1.0, 0.6), 0.0)

    primary = ctx["primary_trend"].values.astype(float)
    align = primary * side                                     # -1 .. +1
    if strategy.with_trend:
        c_tf = np.clip((align + 1.0) / 2.0, 0.0, 1.0)
        conflict = align <= -0.5                               # entering against strong HTF trend
    else:
        # Counter-trend styles: neutral HTF is ideal, a strong opposing trend is a conflict.
        c_tf = np.clip(1.0 - np.maximum(-align, 0.0), 0.0, 1.0) * 0.8 + 0.2 * (align > 0)
        conflict = align <= -0.75
    ratio = np.full(len(side), 0.05) if spread_atr_ratio is None else np.broadcast_to(
        np.asarray(spread_atr_ratio, float), side.shape)
    c_cost = np.clip(1.0 - ratio / 0.15, 0.0, 1.0)
    c_news = np.ones(len(side)) if news_factor is None else np.broadcast_to(np.asarray(news_factor, float),
                                                                            side.shape)
    conf = (WEIGHTS["strength"] * c_strength + WEIGHTS["regime"] * c_regime + WEIGHTS["timeframes"] * c_tf +
            WEIGHTS["cost"] * c_cost + WEIGHTS["news"] * c_news)
    veto = np.zeros(len(side), dtype=bool)
    if meta_prob is not None:
        mp = np.asarray(meta_prob, float)
        has = np.isfinite(mp)
        conf = np.where(has, (1 - META_BLEND) * conf + META_BLEND * mp, conf)
        veto = has & (mp < META_VETO)
    conf = np.where(active, np.clip(conf, 0.0, 1.0), 0.0)
    return pd.DataFrame({"confidence": conf, "c_strength": c_strength, "c_regime": c_regime,
                         "c_timeframes": c_tf, "c_cost": c_cost, "c_news": c_news, "regime_ok": reg_ok,
                         "htf_conflict": conflict & active, "meta_veto": veto & active}, index=signals.index)


def select(strategy: Strategy, signals: pd.DataFrame, ctx: pd.DataFrame, min_confidence: float,
           **score_kw) -> pd.DataFrame:
    """Apply the strategy selector and the confidence threshold.

    Adds ``confidence``, ``accepted`` and ``reject_reason`` to a copy of
    ``signals``. Rejected rows keep their side so the reason can be logged.
    """
    sc = score(strategy, signals, ctx, **score_kw)
    out = signals.copy()
    for col in sc.columns:
        out[col] = sc[col].values
    out["regime"] = ctx["regime"].values
    active = out["side"].values != 0
    reason = np.full(len(out), "", dtype=object)
    reason[active & (out["confidence"].values < min_confidence)] = "confidence below threshold"
    reason[active & out["meta_veto"].values] = "vetoed by validated ML model"
    reason[active & out["htf_conflict"].values] = "against strong higher-timeframe trend"
    reason[active & ~out["regime_ok"].values] = "strategy not suited to current regime"
    reason[active & (out["regime"].values == Regime.VOLATILE.value)] = "disorderly volatility regime"
    reason[active & (out["regime"].values == Regime.UNKNOWN.value)] = "regime unknown (insufficient history)"
    out["reject_reason"] = reason
    out["accepted"] = active & (reason == "")
    return out


CONFLICT_MARGIN = 0.15


def resolve_conflicts(sides: list[int], confidences: list[float]) -> list[bool]:
    """Opposite signals on one symbol at the same time. Keep the clearly
    stronger direction (confidence lead >= CONFLICT_MARGIN); otherwise trade
    neither. Returns a keep-flag per candidate."""
    if len(set(sides)) <= 1:
        return [True] * len(sides)
    best = max(range(len(sides)), key=lambda i: confidences[i])
    rival = max((confidences[i] for i in range(len(sides)) if sides[i] != sides[best]))
    if confidences[best] - rival < CONFLICT_MARGIN:
        return [False] * len(sides)
    return [s == sides[best] for s in sides]
