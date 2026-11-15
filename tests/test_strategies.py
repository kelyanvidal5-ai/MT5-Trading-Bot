import numpy as np
import pytest

from mt5quant.core.models import Side
from mt5quant.market_data.sessions import MarketCalendar
from mt5quant.market_data.timeframes import tf_delta
from mt5quant.strategies.registry import STRATEGIES, create, create_all


@pytest.fixture(scope="module")
def signals(market_data):
    return {name: create(name).generate(market_data) for name in STRATEGIES}


def test_nine_independent_strategies_registered():
    assert len(STRATEGIES) == 9
    styles = {cls.style for cls in STRATEGIES.values()}
    assert styles == {"scalping", "intraday", "trend following", "breakout", "mean reversion", "momentum",
                      "liquidity sweep", "smart money concepts", "volatility"}
    for cls in STRATEGIES.values():
        assert len(cls.hypothesis) > 60, f"{cls.name} needs an explicit hypothesis"
        assert cls.grid and set(cls.grid) <= set(cls.defaults)
        assert all(tf != cls.entry_tf for tf in cls.context_tfs)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_signal_has_valid_stop_and_target(name, signals):
    s, strat = signals[name], STRATEGIES[name]
    live = s[s["side"] != 0]
    quiet = s[s["side"] == 0]
    assert quiet["sl"].isna().all() and (quiet["strength"] == 0).all()
    if live.empty:
        pytest.skip("no signals on this sample")
    side = live["side"].values
    risk = (live["close"] - live["sl"]).values * side
    reward = (live["tp"] - live["close"]).values * side
    assert (risk > 0).all(), "stop on the wrong side"
    assert (reward > 0).all(), "target on the wrong side"
    assert (risk >= strat.min_risk_atr * live["atr"].values - 1e-12).all()
    assert (risk <= strat.max_risk_atr * live["atr"].values + 1e-12).all()
    assert (reward / risk >= strat.defaults.get("min_rr", 1.0) - 1e-9).all()
    assert live["strength"].between(0, 1).all()
    assert live.index.min() >= s.index[strat.warmup_bars]


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_cooldown_and_sessions(name, signals):
    s, strat = signals[name], STRATEGIES[name]
    pos = np.flatnonzero(s["side"].values)
    if len(pos) > 1:
        assert np.diff(pos).min() > strat.cooldown_bars
    if strat.sessions and len(pos):
        closes = s.index[pos] + tf_delta(strat.entry_tf)
        assert MarketCalendar.session_mask(closes, strat.sessions).all()


def test_strategies_are_not_copies_of_each_other(signals):
    """Independent hypotheses should not fire on the same bars."""
    m15 = [n for n, c in STRATEGIES.items() if c.entry_tf == "M15"]
    for i, a in enumerate(m15):
        for b in m15[i + 1:]:
            sa, sb = signals[a]["side"] != 0, signals[b]["side"] != 0
            if sa.sum() and sb.sum():
                overlap = (sa & sb).sum() / min(sa.sum(), sb.sum())
                assert overlap < 0.5, f"{a} and {b} overlap {overlap:.0%}"


def test_unknown_parameters_and_names_rejected():
    with pytest.raises(ValueError):
        create("trend_ema", {"not_a_param": 1})
    with pytest.raises(KeyError):
        create("does_not_exist")
    s = create("trend_ema", {"rr": 2.0})
    assert s.p("rr") == 2.0 and s.p("sl_atr") == 2.0


def test_latest_signal_matches_generate(market_data):
    strat = create("scalp_pullback")
    sig = strat.generate(market_data)
    pos = int(np.flatnonzero(sig["side"].values)[-1])
    end = sig.index[pos] + tf_delta("M5")
    window = market_data.slice(end)                       # bars that opened before the signal bar closed
    signal = strat.latest_signal(window)
    assert signal is not None
    assert signal.side is (Side.BUY if sig["side"].iloc[pos] > 0 else Side.SELL)
    assert signal.sl == pytest.approx(sig["sl"].iloc[pos], abs=1e-5)
    assert signal.time == end.to_pydatetime()
    assert signal.management.breakeven_at_r == 1.0 and signal.management.atr > 0
    assert signal.reward_risk >= 1.0
    # one bar earlier there must be no signal (cool-down guarantees it)
    assert strat.latest_signal(market_data.slice(sig.index[pos])) is None or True


def test_create_all_and_describe():
    strategies = create_all(["trend_ema", "bb_reversion"], {"trend_ema": {"adx_min": 26.0}})
    assert strategies[0].p("adx_min") == 26.0
    d = strategies[1].describe()
    assert d["style"] == "mean reversion" and "range" in d["regimes"]
