import numpy as np
import pandas as pd
import pytest

from mt5quant.indicators import candles, structure, ta


def frame(o, h, l, c, v=None, start="2024-03-12 08:00", freq="15min"):
    idx = pd.date_range(start, periods=len(c), freq=freq, tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v or [100.0] * len(c)},
                        index=idx, dtype=float)


def from_closes(closes, spread=0.5, **kw):
    c = np.asarray(closes, float)
    o = np.concatenate(([c[0]], c[:-1]))
    return frame(o, np.maximum(o, c) + spread, np.minimum(o, c) - spread, c, **kw)


def test_sma_ema_known_values():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    assert ta.sma(s, 3).tolist()[2:] == [2, 3, 4, 5]
    e = ta.ema(s, 3)                                     # alpha = 0.5, seeded at first value
    assert np.isnan(e.iloc[1]) and e.iloc[2] == pytest.approx(2.25) and e.iloc[3] == pytest.approx(3.125)


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert ta.rsi(up, 14).iloc[-1] == 100.0
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert ta.rsi(down, 14).iloc[-1] == pytest.approx(0.0)
    rng = np.random.default_rng(0)
    r = ta.rsi(pd.Series(100 + rng.normal(0, 1, 500).cumsum()), 14).dropna()
    assert r.between(0, 100).all()


def test_rsi_wilder_reference():
    # Classic Wilder example series (first RSI value ~70.46 with SMA seeding; RMA seeding converges nearby).
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
              46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35,
              44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13]
    r = ta.rsi(pd.Series(closes), 14)
    assert 30 < r.iloc[-1] < 45


def test_macd_and_bollinger_identities(bars_m5):
    c = bars_m5["close"].iloc[:3000]
    m = ta.macd(c)
    assert np.allclose((m["macd"] - m["signal"]).dropna(), m["hist"].dropna())
    bb = ta.bollinger(c, 20, 2.0)
    assert np.allclose(bb["mid"].dropna(), c.rolling(20).mean().dropna())
    assert ((bb["upper"] >= bb["mid"]) & (bb["mid"] >= bb["lower"])).iloc[25:].all()
    inside = ((c <= bb["upper"]) & (c >= bb["lower"])).iloc[25:].mean()
    assert 0.80 < inside < 0.99


def test_atr_adx(bars_m5):
    df = bars_m5.iloc[:5000]
    a = ta.atr(df, 14)
    assert (a.dropna() > 0).all()
    tr = ta.true_range(df)
    assert (tr >= df["high"] - df["low"] - 1e-12).all()
    x = ta.adx(df).dropna()
    assert x["adx"].between(0, 100).all() and x["plus_di"].between(0, 100).all()
    # A clean trend gives a high ADX and +DI above -DI.
    trend = from_closes(np.linspace(100, 160, 300), spread=0.1)
    t = ta.adx(trend).iloc[-1]
    assert t["adx"] > 60 and t["plus_di"] > t["minus_di"]


def test_vwap_resets_each_trading_day():
    df = from_closes([100.0] * 40 + [110.0] * 60, freq="1h", start="2024-03-11 12:00")
    v = ta.vwap(df)["vwap"]
    # 17:00 New York on 2024-03-11 = 21:00 UTC: the bar opening there starts a new anchor.
    new_day = df.index.get_loc(pd.Timestamp("2024-03-11 21:00", tz="UTC"))
    tp = (df["high"] + df["low"] + df["close"]) / 3
    assert v.iloc[new_day] == pytest.approx(tp.iloc[new_day])
    assert v.iloc[new_day - 1] == pytest.approx(tp.iloc[:new_day].mean())


def test_donchian_excludes_current_bar():
    df = from_closes([10, 11, 12, 13, 20], spread=0.0)
    ch = ta.donchian(df, 3)
    assert ch["upper"].iloc[-1] == 13.0 and df["close"].iloc[-1] > ch["upper"].iloc[-1]


def test_efficiency_ratio_and_percentile():
    assert ta.efficiency_ratio(pd.Series(np.arange(50, dtype=float)), 10).iloc[-1] == pytest.approx(1.0)
    zig = pd.Series([0.0, 1.0] * 30)
    assert ta.efficiency_ratio(zig, 10).iloc[-1] == pytest.approx(0.0)
    assert ta.rolling_percentile(pd.Series(np.arange(200, dtype=float)), 100).iloc[-1] == 1.0


def test_fibonacci_levels():
    f = ta.fibonacci_levels(100.0, 200.0)
    assert f["0.5"] == 150.0 and f["0.618"] == pytest.approx(138.2) and f["0.382"] == pytest.approx(161.8)


def test_ema_alignment():
    rib = ta.ema_ribbon(pd.Series(np.linspace(1, 2, 800)))
    assert set(rib.columns) == {"ema9", "ema20", "ema50", "ema100", "ema200"}
    assert ta.ema_alignment(rib).iloc[-1] == 1.0


# ------------------------------------------------------------------- candles
def test_engulfing_and_pin_bars():
    df = frame(o=[10, 10, 10.0, 9.0, 10.0], h=[10.5, 10.5, 10.1, 10.6, 10.1],
               l=[9.5, 9.5, 8.9, 8.9, 8.0], c=[10, 10, 9.0, 10.5, 9.9])
    p = candles.candle_patterns(df, atr_n=2)
    assert p["bull_engulfing"].iloc[3] and not p["bear_engulfing"].iloc[3]
    assert p["hammer"].iloc[4]                     # long lower wick, close near the high
    rej = candles.rejection(df)
    assert rej["bull"].iloc[4] and not rej["bear"].iloc[4]


def test_doji_inside_bar():
    df = frame(o=[10, 10.0, 10.0], h=[11, 10.8, 10.5], l=[9, 9.2, 9.5], c=[10.5, 10.6, 10.01])
    p = candles.candle_patterns(df, atr_n=2)
    assert p["inside_bar"].iloc[1] and p["doji"].iloc[2]


# ----------------------------------------------------------------- structure
def zigzag():
    # up-trend with clear pivots: lows 100,104,108 ; highs 110,114,118
    pts = [100, 110, 104, 114, 108, 118, 112, 122]
    closes = []
    for a, b in zip(pts[:-1], pts[1:]):
        closes += list(np.linspace(a, b, 8, endpoint=False))
    return from_closes(closes + [pts[-1]], spread=0.2)


def test_swings_are_confirmed_with_lag():
    df = zigzag()
    sw = structure.swings(df, 3, 3)
    first_conf = int(np.flatnonzero(sw["sh_new"].values)[0])
    pivot_pos = int(sw["last_sh_pos"].iloc[first_conf])
    assert first_conf - pivot_pos == 3                       # known exactly `right` bars later
    assert sw["last_sh"].iloc[first_conf] == df["high"].iloc[pivot_pos]
    assert sw["last_sh"].iloc[:first_conf].isna().all()      # nothing before confirmation


def test_structure_labels_uptrend_and_bos():
    df = zigzag()
    ms = structure.market_structure(df)
    assert ms["hh_hl"].iloc[-1] and not ms["lh_ll"].iloc[-1]
    assert ms["trend"].iloc[-1] == 1
    assert (ms["bos"] == 1).sum() >= 2 and (ms["choch"] != 0).sum() == 0


def test_choch_on_reversal():
    up = zigzag()
    down_closes = list(np.linspace(122, 95, 30))
    df = from_closes(list(up["close"]) + down_closes, spread=0.2)
    ms = structure.market_structure(df)
    assert (ms["choch"] == -1).sum() == 1
    assert ms["trend"].iloc[-1] == -1


def test_fvg_detection_and_invalidation():
    #            c1          c2 (impulse)   c3          retest      break below
    df = frame(o=[10.0, 10.0, 10.2, 12.0, 12.4, 12.3, 11.9],
               h=[10.2, 10.2, 12.1, 12.6, 12.6, 12.4, 12.0],
               l=[9.8, 9.9, 10.1, 11.5, 11.2, 11.8, 9.5],
               c=[10.0, 10.1, 12.0, 12.4, 12.3, 11.9, 9.6])
    f = structure.fair_value_gaps(df, atr_n=2, min_atr=0.1)
    assert f["bull_new"].iloc[3]                              # low(3)=11.5 > high(1)=10.2
    assert f["bull_top"].iloc[3] == 11.5 and f["bull_bot"].iloc[3] == 10.2
    assert f["bull_touches"].iloc[4] == 1                     # low 11.2 traded into the gap
    assert np.isnan(f["bull_top"].iloc[6])                    # close 9.6 < bottom -> invalid


def test_order_block_is_last_down_candle_before_break():
    df = zigzag()
    ms = structure.market_structure(df)
    ob = structure.order_blocks(df, ms)
    i = int(np.flatnonzero(ob["bull_new"].values)[0])
    assert ms["bos"].iloc[i] == 1
    top, bot = ob["bull_top"].iloc[i], ob["bull_bot"].iloc[i]
    j = max(k for k in range(i) if df["close"].iloc[k] < df["open"].iloc[k])
    assert top == df["high"].iloc[j] and bot == df["low"].iloc[j]


def test_liquidity_sweep_of_previous_day_low():
    n = 96 * 2
    closes = [100.0] * n
    df = from_closes(closes, spread=0.3, start="2024-03-12 21:00")     # two full FX days of flat prices
    # third day: wick below the previous day's low (99.7) that closes back above
    sweep_bar = frame(o=[100.0], h=[100.2], l=[98.9], c=[100.1], start=str(df.index[-1] + pd.Timedelta(minutes=15)))
    df = pd.concat([df, sweep_bar])
    ls = structure.liquidity_sweeps(df, atr_n=14, min_pierce_atr=0.05)
    assert ls["sweep"].iloc[-1] == 1
    assert ls["level"].iloc[-1] == pytest.approx(99.7) and ls["extreme"].iloc[-1] == 98.9
    lv = structure.previous_day_levels(df)
    assert lv["pdl"].iloc[-1] == pytest.approx(99.7)


def test_breakout_and_false_breakout():
    closes = [100.0] * 30 + [103.0, 103.5, 99.5]
    df = from_closes(closes, spread=0.2)
    b = structure.breakouts(df, n=20, fail_within=5)
    assert b["breakout"].iloc[30] == 1
    assert b["false_breakout"].iloc[32] == -1                 # closed back under the broken level


def test_support_resistance_and_fib():
    df = zigzag()
    sw = structure.swings(df)
    sr = structure.support_resistance(df, sw)
    last = sr.iloc[-1]
    assert last["support"] < df["close"].iloc[-1]
    fib = structure.fib_retracement(sw)
    row, s = fib.iloc[-1], sw.iloc[-1]
    assert min(s["last_sl"], s["last_sh"]) < row["fib_500"] < max(s["last_sl"], s["last_sh"])
    eq = structure.equal_levels(sw, ta.atr(df))
    assert eq["eq_high"].isna().all()                         # rising highs: no equal highs


def test_supply_demand_zone():
    df = frame(o=[10, 10, 10, 10.0, 10.05, 10.1], h=[10.2, 10.2, 10.2, 10.2, 10.2, 11.6],
               l=[9.8, 9.8, 9.8, 9.8, 9.9, 10.05], c=[10, 10, 10, 10.05, 10.1, 11.5])
    z = structure.supply_demand_zones(df, atr_n=3, impulse_atr=1.5)
    assert z["demand_top"].iloc[-1] == 10.2 and z["demand_bot"].iloc[-1] == 9.9
