from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from mt5quant.ai_engine import anomaly, context, explain, patterns, performance, regime
from mt5quant.ai_engine.meta_model import (FEATURES, MetaModel, build_features, purged_walk_forward_splits)
from mt5quant.core.models import ManagementPlan, Regime, Side, Signal, Tick
from mt5quant.market_data.specs import generic_spec
from mt5quant.market_data.timeframes import MarketData
from mt5quant.strategies.registry import create

UTC = timezone.utc


def ohlc(closes, noise=0.0002, start="2024-01-02", freq="1h", seed=0):
    rng = np.random.default_rng(seed)
    c = np.asarray(closes, float)
    o = np.concatenate(([c[0]], c[:-1]))
    idx = pd.date_range(start, periods=len(c), freq=freq, tz="UTC")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + rng.uniform(0, noise, len(c)),
                         "low": np.minimum(o, c) - rng.uniform(0, noise, len(c)), "close": c,
                         "volume": 100.0}, index=idx)


# -------------------------------------------------------------------- regime
def test_regime_trend_up_down_and_range():
    rng = np.random.default_rng(1)
    n = 600
    noise = rng.normal(0, 0.0004, n).cumsum()
    up = regime.classify(ohlc(1.10 + np.linspace(0, 0.08, n) + noise * 0.3))
    assert up["regime"].iloc[-1] == Regime.TREND_UP.value and up["direction"].iloc[-1] == 1
    down = regime.classify(ohlc(1.10 - np.linspace(0, 0.08, n) + noise * 0.3))
    assert down["regime"].iloc[-1] == Regime.TREND_DOWN.value
    flat = regime.classify(ohlc(1.10 + rng.normal(0, 0.0005, n)))      # mean-reverting noise
    assert flat["regime"].iloc[-50:].isin([Regime.RANGE.value, Regime.QUIET.value]).mean() > 0.8
    assert (up["regime"].iloc[:30] == Regime.UNKNOWN.value).all()


def test_regime_flags_volatility_shock():
    rng = np.random.default_rng(2)
    n = 500
    c = 1.10 + rng.normal(0, 0.0003, n).cumsum()
    df = ohlc(c)
    df.iloc[-1, df.columns.get_loc("high")] += 0.02            # a 2-big-figure spike bar
    out = regime.classify(df)
    assert out["regime"].iloc[-1] == Regime.VOLATILE.value
    assert out["shock_z"].iloc[-1] > 6


def test_regime_and_context_are_causal(market_data, bars_m5):
    h1 = market_data.frame("H1")
    full = regime.classify(h1)
    part = regime.classify(h1.iloc[:1500])
    pd.testing.assert_frame_equal(part, full.iloc[:1500])
    spec = generic_spec("EURUSD")
    cut = 30_000
    ctx_full = context.build_context(MarketData.from_base("EURUSD", spec, bars_m5, "M5"), "M15")
    part_md = MarketData.from_base("EURUSD", spec, bars_m5.iloc[:cut], "M5")
    ctx_part = context.build_context(part_md, "M15")
    known = ctx_part.index + pd.Timedelta(minutes=15) <= bars_m5.index[cut - 1] + pd.Timedelta(minutes=5)
    pd.testing.assert_frame_equal(ctx_part[known], ctx_full.loc[ctx_part.index[known]])


# ------------------------------------------------------- context / selection
def test_selector_rejects_wrong_regime_and_conflicts(market_data):
    strat = create("trend_ema")
    sig = strat.generate(market_data)
    ctx = context.build_context(market_data, "H1")
    sel = context.select(strat, sig, ctx, min_confidence=0.55)
    live = sel[sel["side"] != 0]
    assert len(live) > 10
    acc = live[live["accepted"]]
    assert acc["regime"].isin(["trend_up", "trend_down"]).all()
    # accepted trend trades agree with the regime direction
    assert (np.sign(acc["side"]) == np.sign(ctx.loc[acc.index, "regime_dir"])).all()
    assert (acc["confidence"] >= 0.55).all() and (acc["reject_reason"] == "").all()
    rej = live[~live["accepted"]]
    assert (rej["reject_reason"] != "").all()
    assert not sel.loc[sel["regime"] == "volatile", "accepted"].any()
    # nothing is "accepted" on bars without a signal
    assert not sel.loc[sel["side"] == 0, "accepted"].any()


def test_confidence_components_and_meta_blend(market_data):
    strat = create("scalp_pullback")
    sig = strat.generate(market_data)
    ctx = context.build_context(market_data, "M5")
    base = context.score(strat, sig, ctx)
    live = (sig["side"] != 0).values
    assert base["confidence"][live].between(0, 1).all() and (base["confidence"][~live] == 0).all()
    # wider spread -> lower confidence ; news penalty -> lower confidence
    costly = context.score(strat, sig, ctx, spread_atr_ratio=0.15)
    assert (costly["confidence"][live] < base["confidence"][live]).all()
    news = context.score(strat, sig, ctx, news_factor=0.0)
    assert (news["confidence"][live] < base["confidence"][live]).all()
    # a validated model with low probability vetoes
    low = context.select(strat, sig, ctx, 0.3, meta_prob=np.full(len(sig), 0.2))
    assert not low["accepted"].any()
    assert (low.loc[live, "reject_reason"] != "").all()
    nan = context.select(strat, sig, ctx, 0.3, meta_prob=np.full(len(sig), np.nan))
    assert nan["accepted"].sum() == context.select(strat, sig, ctx, 0.3)["accepted"].sum()


def test_counter_trend_strategy_blocked_against_strong_htf_trend():
    strat = create("bb_reversion")
    idx = pd.date_range("2024-01-02", periods=3, freq="15min", tz="UTC")
    sig = pd.DataFrame({"side": [1, 1, -1], "sl": 1.0, "tp": 1.2, "strength": 0.6, "atr": 0.001, "close": 1.1},
                       index=idx)
    ctx = pd.DataFrame({"regime": "range", "regime_dir": 0.0, "primary_trend": [-0.9, 0.0, -0.9]}, index=idx)
    sel = context.select(strat, sig, ctx, 0.5)
    assert sel["reject_reason"].iloc[0] == "against strong higher-timeframe trend"
    assert sel["accepted"].iloc[1] and sel["accepted"].iloc[2]


# ------------------------------------------------------------------- anomaly
def test_anomaly_detection():
    rng = np.random.default_rng(3)
    df = ohlc(1.10 + rng.normal(0, 0.0003, 300).cumsum())
    df["spread"] = 8.0
    spec = generic_spec("EURUSD")
    now = datetime(2024, 3, 13, 12, 0, tzinfo=UTC)
    ok = anomaly.check_market(df, Tick("EURUSD", now - timedelta(seconds=2), 1.1, 1.10008), spec, now, 8.0)
    assert not ok.blocked
    wide = anomaly.check_market(df, Tick("EURUSD", now, 1.1, 1.1006), spec, now, 8.0)
    assert any("spread" in f for f in wide.flags)
    stale = anomaly.check_market(df, Tick("EURUSD", now - timedelta(minutes=10), 1.1, 1.10008), spec, now, 8.0)
    assert any("stale" in f for f in stale.flags)
    crossed = anomaly.check_market(df, Tick("EURUSD", now, 1.1, 1.1), spec, now, 8.0)
    assert any("crossed" in f for f in crossed.flags)
    shock = df.copy()
    shock.iloc[-1, shock.columns.get_loc("high")] += 0.03
    assert any("volatility shock" in f for f in anomaly.check_market(shock, None, spec, now).flags)
    gap = df.copy()
    gap.iloc[-1, gap.columns.get_loc("open")] += 0.02
    gap.iloc[-1, gap.columns.get_loc("high")] += 0.02
    assert any("gap" in f for f in anomaly.check_market(gap, None, spec, now).flags)


# ---------------------------------------------------------------- meta model
def synthetic_trades(n=900, edge=True, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=FEATURES)
    entry = (pd.Timestamp("2022-01-03", tz="UTC") + pd.to_timedelta(np.arange(n) * 6, unit="h")).values
    exit_ = entry + np.timedelta64(5, "h")
    p = 1 / (1 + np.exp(-(1.4 * X["adx_h1"] - 1.0 * X["vol_rank"]))) if edge else np.full(n, 0.45)
    win = rng.random(n) < p
    r = np.where(win, 1.5, -1.0)
    return X, r, entry, exit_


def test_purged_splits_never_train_on_overlapping_or_future_labels():
    n = 500
    entry = (pd.Timestamp("2022-01-03", tz="UTC") + pd.to_timedelta(np.arange(n), unit="h")).values
    exit_ = entry + np.timedelta64(30, "h")                    # long-lived labels overlap many entries
    folds = list(purged_walk_forward_splits(entry, exit_, 5, embargo=pd.Timedelta(hours=12)))
    assert len(folds) >= 4        # the earliest block is dropped when purging leaves too little to train on
    for train, test in folds:
        assert train.max() < test.min()
        assert exit_[train].max() <= entry[test.min()] - np.timedelta64(12, "h")
        assert test.min() - train.max() >= 42                  # 30h label + 12h embargo purged


def test_meta_model_validates_real_edge_and_rejects_noise(tmp_path):
    X, r, entry, exit_ = synthetic_trades(edge=True)
    good = MetaModel("demo")
    rep = good.fit(X, r, entry, exit_)
    assert rep.validated and rep.oos_auc > 0.6 and good.usable
    assert rep.filtered_expectancy_r > rep.baseline_expectancy_r
    probs = good.predict(X.iloc[:10])
    assert ((probs >= 0) & (probs <= 1)).all()

    Xn, rn, en, xn = synthetic_trades(edge=False, seed=5)
    noise = MetaModel("noise")
    rep_n = noise.fit(Xn, rn, en, xn)
    assert not rep_n.validated and not noise.usable and rep_n.reasons
    assert rep_n.oos_auc < 0.56

    small = MetaModel("small")
    assert not small.fit(X.iloc[:100], r[:100], entry[:100], exit_[:100]).validated

    good.save(tmp_path)
    loaded = MetaModel.load(tmp_path, "demo")
    assert loaded.usable and np.allclose(loaded.predict(X.iloc[:10]), probs)
    assert MetaModel.load(tmp_path, "missing") is None
    with open(tmp_path / "meta_demo.joblib", "ab") as fh:      # tamper with the artefact
        fh.write(b"x")
    with pytest.raises(RuntimeError, match="does not match"):
        MetaModel.load(tmp_path, "demo")


def test_build_features_uses_only_signal_time_columns(market_data):
    strat = create("liquidity_sweep")
    sig = strat.generate(market_data)
    ctx = context.build_context(market_data, "M15")
    f = build_features(sig, ctx)
    assert list(f.columns) == FEATURES and len(f) == int((sig["side"] != 0).sum())
    assert np.isfinite(f.values).all()
    assert (f["risk_atr"] > 0).all() and (f["reward_risk"] > 0).all()


# --------------------------------------------------------------- performance
def test_performance_stats_and_streak_probability():
    s = performance.stats_r([1.5, -1, -1, 2, -1, 1.5])
    assert s["trades"] == 6 and s["win_rate"] == 0.5 and s["expectancy_r"] == pytest.approx(1 / 3)
    assert s["profit_factor"] == pytest.approx(5 / 3) and s["max_losing_streak"] == 2
    assert s["max_drawdown_r"] == pytest.approx(2.0)
    assert performance.prob_losing_streak(8, 8, 0.5) == pytest.approx(0.5 ** 8)
    assert performance.prob_losing_streak(5, 6, 0.5) == 0.0
    assert performance.prob_losing_streak(200, 3, 0.6) > 0.99


def test_degradation_detection():
    rng = np.random.default_rng(0)
    baseline = {"expectancy_r": 0.25, "win_rate": 0.5, "mc_dd95_r": 9.0}
    healthy = list(np.where(rng.random(60) < 0.5, 1.5, -1.0))
    assert performance.evaluate("s", healthy, baseline).status in (performance.HEALTHY, performance.WATCH)
    bad = list(np.where(rng.random(60) < 0.2, 1.5, -1.0))
    h = performance.evaluate("s", bad, baseline)
    assert h.status == performance.DEGRADED and h.reasons
    assert performance.evaluate("s", [-1.0] * 5, baseline).status == performance.INSUFFICIENT
    streak = [1.5, -1] * 10 + [-1.0] * 12
    assert performance.evaluate("s", streak, baseline).status == performance.DEGRADED
    ranked = performance.compare({"a": healthy, "b": bad})
    assert ranked[0]["strategy"] == "a" and "t_stat" in ranked[0]


# ------------------------------------------------------------------ patterns
def test_benjamini_hochberg():
    mask = patterns.benjamini_hochberg(np.array([0.001, 0.02, 0.04, 0.5, 0.9]), alpha=0.05)
    assert mask.tolist() == [True, True, False, False, False]


def test_pattern_miner_finds_planted_effect_and_not_noise():
    rng = np.random.default_rng(4)
    n = 24 * 400
    idx = pd.date_range("2022-01-03", periods=n, freq="1h", tz="UTC")
    ret = rng.normal(0, 0.0005, n)
    noise_close = 1.1 * np.exp(np.cumsum(ret))
    df = pd.DataFrame({"open": noise_close, "high": noise_close * 1.0003, "low": noise_close * 0.9997,
                       "close": noise_close, "volume": 100.0}, index=idx)
    res = patterns.mine(df, horizon=1)
    assert res["significant_after_fdr"].sum() <= 1               # pure noise: essentially nothing survives
    planted = ret.copy()
    planted[idx.hour == 14] += 0.0004                            # the 13:00 bar is followed by a drift
    c2 = 1.1 * np.exp(np.cumsum(planted))
    df2 = pd.DataFrame({"open": c2, "high": c2 * 1.0003, "low": c2 * 0.9997, "close": c2, "volume": 100.0},
                       index=idx)
    res2 = patterns.mine(df2, horizon=1)
    top = res2.iloc[0]
    assert top["kind"] == "hour_utc" and top["pattern"] == "13:00" and top["significant_after_fdr"]


# ------------------------------------------------------------------- explain
def test_explanation_contains_only_computed_facts():
    sig = Signal(strategy="trend_ema", symbol="EURUSD", timeframe="H1", time=datetime(2024, 3, 13, 12, tzinfo=UTC),
                 side=Side.BUY, sl=1.0950, tp=1.1150, ref_price=1.1000, strength=0.7,
                 reasons=["Trends persist."], management=ManagementPlan(atr=0.002), meta={"style": "trend following"})
    text = explain.explain(
        sig, action="executed", regime="trend_up", confidence=0.71, min_confidence=0.55,
        components={"c_strength": 0.7, "c_regime": 1.0, "c_timeframes": 0.8, "c_cost": 0.9, "c_news": 1.0},
        risk={"volume": 0.05, "risk_amount": 25.0, "risk_pct": 0.25, "scale": 0.5, "scale_reason": "news window"},
        context={"adx_h1": 31.2, "er_h1": 0.45, "vol_rank": 0.6, "trends": {"H1": 1, "H4": 1, "D1": 0}},
        news="USD CPI in 95 min (high impact, confirmed schedule)")
    for expected in ("EXECUTED BUY EURUSD via trend_ema", "orderly up-trend", "ADX 31", "Confidence 0.71",
                     "stop 1.09500", "target 1.11500", "reward/risk 3.00", "0.05 lots", "0.25% of equity",
                     "news window", "USD CPI", "H4 up", "D1 flat"):
        assert expected in text
    rejected = explain.explain(sig, action="rejected", regime="volatile", rejections=["daily loss limit reached"])
    assert "Not traded because: daily loss limit reached" in rejected
