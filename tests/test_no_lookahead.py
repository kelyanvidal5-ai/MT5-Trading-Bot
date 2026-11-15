"""Look-ahead bias guard.

For every indicator and every strategy: compute on the full history, then
again on a history truncated at several points. Every value that was already
knowable at the truncation point must be IDENTICAL in both runs. Any feature
that peeks at future bars fails this test.
"""
import numpy as np
import pandas as pd
import pytest

from mt5quant.indicators import candles, structure, ta
from mt5quant.market_data.specs import generic_spec
from mt5quant.market_data.timeframes import MarketData, tf_delta
from mt5quant.strategies.registry import STRATEGIES, create

CUTS = (12_345, 23_456, 31_007)

INDICATORS = {
    "ema": lambda d: ta.ema(d["close"], 50),
    "rsi": lambda d: ta.rsi(d["close"]),
    "macd": lambda d: ta.macd(d["close"]),
    "bollinger": lambda d: ta.bollinger(d["close"]),
    "atr": lambda d: ta.atr(d),
    "adx": lambda d: ta.adx(d),
    "vwap": lambda d: ta.vwap(d),
    "donchian": lambda d: ta.donchian(d),
    "keltner": lambda d: ta.keltner(d),
    "efficiency": lambda d: ta.efficiency_ratio(d["close"]),
    "percentile": lambda d: ta.rolling_percentile(d["close"]),
    "volume": lambda d: ta.volume_profile(d),
    "ribbon": lambda d: ta.ema_ribbon(d["close"]),
    "candles": lambda d: candles.candle_patterns(d),
    "rejection": lambda d: candles.rejection(d),
    "swings": lambda d: structure.swings(d),
    "structure": lambda d: structure.market_structure(d),
    "prev_day": lambda d: structure.previous_day_levels(d),
    "sweeps": lambda d: structure.liquidity_sweeps(d),
    "fvg": lambda d: structure.fair_value_gaps(d),
    "order_blocks": lambda d: structure.order_blocks(d),
    "supply_demand": lambda d: structure.supply_demand_zones(d),
    "sr": lambda d: structure.support_resistance(d),
    "fib": lambda d: structure.fib_retracement(structure.swings(d)),
    "breakouts": lambda d: structure.breakouts(d),
    "equal_levels": lambda d: structure.equal_levels(structure.swings(d), ta.atr(d)),
}


@pytest.fixture(scope="module")
def m15(bars_m5):
    from mt5quant.market_data.timeframes import resample
    return resample(bars_m5.iloc[:36_000], "M15")


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_indicator_is_causal(name, m15):
    fn = INDICATORS[name]
    full = fn(m15)
    for cut in (3_000, 7_777):
        part = fn(m15.iloc[:cut])
        ref = full.iloc[:cut]
        if isinstance(part, pd.Series):
            pd.testing.assert_series_equal(part, ref, check_names=False, rtol=1e-9, atol=1e-12)
        else:
            pd.testing.assert_frame_equal(part, ref, rtol=1e-9, atol=1e-12)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_strategy_signals_do_not_depend_on_future_bars(name, bars_m5):
    spec = generic_spec("EURUSD")
    strategy = create(name)
    full = strategy.generate(MarketData.from_base("EURUSD", spec, bars_m5.iloc[:36_000], "M5"))
    step = tf_delta(strategy.entry_tf)
    compared = 0
    for cut in CUTS:
        part_bars = bars_m5.iloc[:cut]
        known_until = part_bars.index[-1] + tf_delta("M5")          # close time of the last M5 bar
        part = strategy.generate(MarketData.from_base("EURUSD", spec, part_bars, "M5"))
        done = part.index + step <= known_until                       # entry bars that had closed
        a, b = part[done], full.loc[part.index[done]]
        assert (a["side"].values == b["side"].values).all(), f"{name}: signal changed with future data"
        live = a["side"].values != 0
        np.testing.assert_allclose(a["sl"].values[live], b["sl"].values[live], rtol=1e-12)
        np.testing.assert_allclose(a["tp"].values[live], b["tp"].values[live], rtol=1e-12)
        np.testing.assert_allclose(a["strength"].values[live], b["strength"].values[live], rtol=1e-9)
        compared += int(live.sum())
    assert compared > 0 or name in {"bb_reversion", "vol_squeeze"}, f"{name}: no signals to compare"


def test_guard_actually_detects_a_leaky_feature(m15):
    """Meta-test: a centred (future-peeking) window must trip the guard."""
    def leaky(d):
        return d["close"].rolling(5, center=True).mean()

    with pytest.raises(AssertionError):
        pd.testing.assert_series_equal(leaky(m15.iloc[:3000]), leaky(m15).iloc[:3000])
