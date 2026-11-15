"""Price-action and Smart Money Concepts primitives.

Causality rule used throughout: a swing point at bar ``j`` needs ``right``
later bars to be confirmed, so it only becomes visible at bar ``j + right``.
Everything derived from swings (structure, BOS/CHoCH, sweeps, order blocks,
support/resistance, Fibonacci legs) therefore lags by construction and never
uses a bar that has not closed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..market_data.timeframes import _trading_period_open
from .ta import atr, donchian

NAN = np.nan


# --------------------------------------------------------------------- swings
def swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Confirmed swing highs/lows.

    Columns (all "as known at the close of each bar"):
      sh_new / sl_new    True on the bar where a swing gets CONFIRMED
      last_sh / last_sl  price of the most recent confirmed swing high/low
      prev_sh / prev_sl  the confirmed swing before that
      last_sh_pos / last_sl_pos   bar position of the pivot itself
    """
    high, low = df["high"], df["low"]
    win = left + right + 1
    # Rolling window ending at bar i covers the pivot candidate at i-right.
    cand_h, cand_l = high.shift(right), low.shift(right)
    sh_new = (high.rolling(win, min_periods=win).max() == cand_h) & \
             (cand_h > high.rolling(right, min_periods=right).max())          # strictly above the right side
    sl_new = (low.rolling(win, min_periods=win).min() == cand_l) & \
             (cand_l < low.rolling(right, min_periods=right).min())
    n = len(df)
    pos = np.arange(n, dtype=float)
    out = pd.DataFrame(index=df.index)
    out["sh_new"], out["sl_new"] = sh_new.fillna(False), sl_new.fillna(False)
    for tag, flag, price in (("sh", out["sh_new"], cand_h), ("sl", out["sl_new"], cand_l)):
        ev_price = price.where(flag)
        out[f"last_{tag}"] = ev_price.ffill()
        out[f"last_{tag}_pos"] = pd.Series(np.where(flag.values, pos - right, NAN), index=df.index).ffill()
        events = ev_price.dropna()
        out[f"prev_{tag}"] = events.shift(1).reindex(df.index).where(flag).ffill()
    return out


def market_structure(df: pd.DataFrame, sw: pd.DataFrame | None = None, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Trend from swing sequence plus break-of-structure / change-of-character.

    ``trend``   +1 after a bullish break, -1 after a bearish break, 0 unknown
    ``bos``     +1/-1 on the bar whose CLOSE breaks structure in trend direction
    ``choch``   +1/-1 on the bar whose CLOSE breaks structure AGAINST the trend
                (a market-structure shift)
    ``hh_hl`` / ``lh_ll``  swing-sequence labels (higher-high & higher-low...)
    ``break_level``  the swing level that was broken on that bar
    """
    sw = sw if sw is not None else swings(df, left, right)
    close = df["close"].values
    last_sh, last_sl = sw["last_sh"].values, sw["last_sl"].values
    n = len(df)
    trend = np.zeros(n, dtype=np.int8)
    bos = np.zeros(n, dtype=np.int8)
    choch = np.zeros(n, dtype=np.int8)
    level = np.full(n, NAN)
    cur, broken_h, broken_l = 0, NAN, NAN
    for i in range(n):
        sh, sl = last_sh[i], last_sl[i]
        if sh == sh and close[i] > sh and sh != broken_h:
            if cur == -1:
                choch[i] = 1
            else:
                bos[i] = 1
            cur, broken_h, level[i] = 1, sh, sh
        elif sl == sl and close[i] < sl and sl != broken_l:
            if cur == 1:
                choch[i] = -1
            else:
                bos[i] = -1
            cur, broken_l, level[i] = -1, sl, sl
        trend[i] = cur
    out = pd.DataFrame({"trend": trend, "bos": bos, "choch": choch, "break_level": level}, index=df.index)
    out["hh_hl"] = (sw["last_sh"] > sw["prev_sh"]) & (sw["last_sl"] > sw["prev_sl"])
    out["lh_ll"] = (sw["last_sh"] < sw["prev_sh"]) & (sw["last_sl"] < sw["prev_sl"])
    out["swing_trend"] = np.where(out["hh_hl"], 1, np.where(out["lh_ll"], -1, 0))
    return out


# ------------------------------------------------------------------ liquidity
def previous_day_levels(df: pd.DataFrame) -> pd.DataFrame:
    """High/low of the previous completed FX trading day, plus the running
    high/low of the current day *up to the previous bar*."""
    day = pd.Series(_trading_period_open(df.index, weekly=False), index=df.index)
    daily_h = df["high"].groupby(day).max()
    daily_l = df["low"].groupby(day).min()
    out = pd.DataFrame(index=df.index)
    out["pdh"] = day.map(daily_h.shift(1))
    out["pdl"] = day.map(daily_l.shift(1))
    out["day_high_so_far"] = df["high"].groupby(day).cummax().groupby(day).shift(1)
    out["day_low_so_far"] = df["low"].groupby(day).cummin().groupby(day).shift(1)
    return out


def liquidity_sweeps(df: pd.DataFrame, sw: pd.DataFrame | None = None, atr_n: int = 14,
                     min_pierce_atr: float = 0.05, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Stop-runs: the bar trades THROUGH a resting-liquidity level (previous
    day's extreme or a confirmed swing) and CLOSES back inside.

    ``sweep`` = +1 bullish (sell-side liquidity taken below a low, close back
    above), -1 bearish. ``level`` is the level swept, ``extreme`` the wick tip.
    """
    sw = sw if sw is not None else swings(df, left, right)
    pd_lv = previous_day_levels(df)
    a = atr(df, atr_n)
    pierce = min_pierce_atr * a
    # Use the swing levels known at the PREVIOUS bar: the sweep bar itself
    # must not be the bar that confirms the level.
    lows = pd.concat([pd_lv["pdl"], sw["last_sl"].shift(1)], axis=1)
    highs = pd.concat([pd_lv["pdh"], sw["last_sh"].shift(1)], axis=1)
    bull_level = pd.Series(NAN, index=df.index)
    bear_level = pd.Series(NAN, index=df.index)
    for col in lows.columns:
        lv = lows[col]
        hit = (df["low"] < lv - pierce) & (df["close"] > lv) & (df["open"] > lv - pierce)
        bull_level = bull_level.where(~hit | bull_level.notna(), lv)
    for col in highs.columns:
        lv = highs[col]
        hit = (df["high"] > lv + pierce) & (df["close"] < lv) & (df["open"] < lv + pierce)
        bear_level = bear_level.where(~hit | bear_level.notna(), lv)
    sweep = np.where(bull_level.notna(), 1, np.where(bear_level.notna(), -1, 0))
    both = bull_level.notna() & bear_level.notna()
    sweep = np.where(both, 0, sweep)
    out = pd.DataFrame({"sweep": sweep}, index=df.index)
    out["level"] = np.where(sweep == 1, bull_level, np.where(sweep == -1, bear_level, NAN))
    out["extreme"] = np.where(sweep == 1, df["low"], np.where(sweep == -1, df["high"], NAN))
    return out


def equal_levels(sw: pd.DataFrame, a: pd.Series, tol_atr: float = 0.15) -> pd.DataFrame:
    """Liquidity pools: two consecutive confirmed swings at (nearly) the same
    price - clustered stops rest just beyond them."""
    tol = tol_atr * a
    eqh = (sw["last_sh"] - sw["prev_sh"]).abs() <= tol
    eql = (sw["last_sl"] - sw["prev_sl"]).abs() <= tol
    return pd.DataFrame({"eq_high": sw[["last_sh", "prev_sh"]].max(axis=1).where(eqh),
                         "eq_low": sw[["last_sl", "prev_sl"]].min(axis=1).where(eql)})


# ------------------------------------------------------------ zones (FVG/OB)
def _track_zones(n: int, create_at: np.ndarray, top: np.ndarray, bot: np.ndarray, close: np.ndarray,
                 high: np.ndarray, low: np.ndarray, bullish: bool, max_age: int) -> tuple[np.ndarray, ...]:
    """Keep the most recent still-valid zone. A bullish zone dies when a bar
    closes below its bottom (bearish: above its top) or when it gets too old.
    ``touched`` counts bars that traded into the zone after creation."""
    z_top, z_bot = np.full(n, NAN), np.full(n, NAN)
    z_age = np.full(n, NAN)
    z_touch = np.zeros(n, dtype=np.int32)
    cur_top = cur_bot = NAN
    born, touches = -1, 0
    for i in range(n):
        if born >= 0:
            dead = close[i] < cur_bot if bullish else close[i] > cur_top
            if dead or i - born > max_age:
                born = -1
            elif (low[i] <= cur_top if bullish else high[i] >= cur_bot):
                touches += 1
        if create_at[i]:
            cur_top, cur_bot, born, touches = top[i], bot[i], i, 0
        if born >= 0:
            z_top[i], z_bot[i], z_age[i], z_touch[i] = cur_top, cur_bot, i - born, touches
    return z_top, z_bot, z_age, z_touch


def fair_value_gaps(df: pd.DataFrame, atr_n: int = 14, min_atr: float = 0.25, max_age: int = 60) -> pd.DataFrame:
    """Three-candle imbalances. A bullish FVG exists when candle 3's low is
    above candle 1's high; it is known at the close of candle 3."""
    a = atr(df, atr_n).values
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    h2, l2 = df["high"].shift(2).values, df["low"].shift(2).values
    with np.errstate(invalid="ignore"):
        bull_new = (low > h2) & ((low - h2) >= min_atr * a)
        bear_new = (high < l2) & ((l2 - high) >= min_atr * a)
    n = len(df)
    bt, bb, ba, bto = _track_zones(n, bull_new, low, h2, close, high, low, True, max_age)
    st, sb, sa, sto = _track_zones(n, bear_new, l2, high, close, high, low, False, max_age)
    return pd.DataFrame({"bull_new": bull_new, "bear_new": bear_new, "bull_top": bt, "bull_bot": bb,
                         "bull_age": ba, "bull_touches": bto, "bear_top": st, "bear_bot": sb, "bear_age": sa,
                         "bear_touches": sto}, index=df.index)


def order_blocks(df: pd.DataFrame, ms: pd.DataFrame | None = None, lookback: int = 12, max_age: int = 80,
                 left: int = 3, right: int = 3) -> pd.DataFrame:
    """Order blocks: the last opposite-coloured candle before the impulse
    that broke structure. Bullish OB = last down candle before a bullish
    BOS/CHoCH; its range is the zone institutions are assumed to defend."""
    ms = ms if ms is not None else market_structure(df, left=left, right=right)
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    brk = (ms["bos"].values + ms["choch"].values)
    n = len(df)
    bull_new, bear_new = np.zeros(n, bool), np.zeros(n, bool)
    b_top, b_bot = np.full(n, NAN), np.full(n, NAN)
    s_top, s_bot = np.full(n, NAN), np.full(n, NAN)
    for i in np.flatnonzero(brk != 0):
        want_down = brk[i] > 0
        for j in range(i - 1, max(i - lookback, 0) - 1, -1):
            if (c[j] < o[j]) if want_down else (c[j] > o[j]):
                if want_down:
                    bull_new[i], b_top[i], b_bot[i] = True, h[j], l[j]
                else:
                    bear_new[i], s_top[i], s_bot[i] = True, h[j], l[j]
                break
    bt, bb, ba, bto = _track_zones(n, bull_new, b_top, b_bot, c, h, l, True, max_age)
    st, sb, sa, sto = _track_zones(n, bear_new, s_top, s_bot, c, h, l, False, max_age)
    return pd.DataFrame({"bull_new": bull_new, "bear_new": bear_new, "bull_top": bt, "bull_bot": bb,
                         "bull_age": ba, "bull_touches": bto, "bear_top": st, "bear_bot": sb, "bear_age": sa,
                         "bear_touches": sto}, index=df.index)


def supply_demand_zones(df: pd.DataFrame, atr_n: int = 14, impulse_atr: float = 1.5, max_age: int = 120) -> pd.DataFrame:
    """Demand zone: a small "base" candle followed by an explosive up candle
    (body >= ``impulse_atr`` x ATR). Supply is the mirror image."""
    a = atr(df, atr_n)
    body = df["close"] - df["open"]
    base_small = (df["high"].shift(1) - df["low"].shift(1)) <= 0.9 * a
    demand_new = ((body >= impulse_atr * a) & base_small).fillna(False).values
    supply_new = ((-body >= impulse_atr * a) & base_small).fillna(False).values
    ph, pl = df["high"].shift(1).values, df["low"].shift(1).values
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    dt, db, da, dto = _track_zones(n, demand_new, ph, pl, c, h, l, True, max_age)
    st, sb, sa, sto = _track_zones(n, supply_new, ph, pl, c, h, l, False, max_age)
    return pd.DataFrame({"demand_top": dt, "demand_bot": db, "demand_age": da, "demand_touches": dto,
                         "supply_top": st, "supply_bot": sb, "supply_age": sa, "supply_touches": sto},
                        index=df.index)


# -------------------------------------------------------- support/resistance
def support_resistance(df: pd.DataFrame, sw: pd.DataFrame | None = None, keep: int = 12, merge_atr: float = 0.3,
                       atr_n: int = 14, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Nearest support below / resistance above the close, built by clustering
    the last ``keep`` confirmed swing prices. ``*_touches`` is the number of
    swings merged into that level (a crude strength measure)."""
    sw = sw if sw is not None else swings(df, left, right)
    a = atr(df, atr_n).values
    close = df["close"].values
    sh_new, sl_new = sw["sh_new"].values, sw["sl_new"].values
    last_sh, last_sl = sw["last_sh"].values, sw["last_sl"].values
    n = len(df)
    sup, res = np.full(n, NAN), np.full(n, NAN)
    sup_t, res_t = np.zeros(n, np.int32), np.zeros(n, np.int32)
    levels: list[list[float]] = []            # [price, touches]
    for i in range(n):
        for flag, price in ((sh_new[i], last_sh[i]), (sl_new[i], last_sl[i])):
            if flag and a[i] == a[i]:
                for lv in levels:
                    if abs(lv[0] - price) <= merge_atr * a[i]:
                        lv[0] = (lv[0] * lv[1] + price) / (lv[1] + 1)
                        lv[1] += 1
                        break
                else:
                    levels.append([price, 1])
                    if len(levels) > keep:
                        levels.pop(0)
        best_s = best_r = None
        for lv in levels:
            if lv[0] < close[i] and (best_s is None or lv[0] > best_s[0]):
                best_s = lv
            elif lv[0] > close[i] and (best_r is None or lv[0] < best_r[0]):
                best_r = lv
        if best_s:
            sup[i], sup_t[i] = best_s[0], best_s[1]
        if best_r:
            res[i], res_t[i] = best_r[0], best_r[1]
    return pd.DataFrame({"support": sup, "resistance": res, "support_touches": sup_t,
                         "resistance_touches": res_t}, index=df.index)


def fib_retracement(sw: pd.DataFrame) -> pd.DataFrame:
    """Retracement levels of the latest confirmed swing leg. ``leg`` = +1 when
    the most recent confirmed pivot is a high (up-leg low->high)."""
    up_leg = sw["last_sh_pos"] > sw["last_sl_pos"]
    span = sw["last_sh"] - sw["last_sl"]
    out = pd.DataFrame({"leg": np.where(up_leg, 1, -1)}, index=sw.index)
    for name, r in (("fib_382", 0.382), ("fib_500", 0.5), ("fib_618", 0.618)):
        out[name] = np.where(up_leg, sw["last_sh"] - r * span, sw["last_sl"] + r * span)
    out.loc[span.isna() | (span <= 0), ["fib_382", "fib_500", "fib_618"]] = NAN
    return out


# ------------------------------------------------------------------ breakouts
def breakouts(df: pd.DataFrame, n: int = 20, fail_within: int = 5) -> pd.DataFrame:
    """Channel breakouts and failed ("false") breakouts.

    ``breakout``        +1/-1 on a close outside the previous ``n``-bar channel
    ``false_breakout``  -1 when a recent upside breakout closes back below the
                        broken level within ``fail_within`` bars (bull trap);
                        +1 for a failed downside breakout (bear trap)
    """
    ch = donchian(df, n)
    up = (df["close"] > ch["upper"]) & (df["close"].shift(1) <= ch["upper"].shift(1))
    dn = (df["close"] < ch["lower"]) & (df["close"].shift(1) >= ch["lower"].shift(1))
    up_level = ch["upper"].where(up).ffill(limit=fail_within)
    dn_level = ch["lower"].where(dn).ffill(limit=fail_within)
    failed_up = (~up) & up_level.notna() & (df["close"] < up_level) & (df["close"].shift(1) >= up_level)
    failed_dn = (~dn) & dn_level.notna() & (df["close"] > dn_level) & (df["close"].shift(1) <= dn_level)
    return pd.DataFrame({
        "breakout": np.where(up, 1, np.where(dn, -1, 0)),
        "false_breakout": np.where(failed_up, -1, np.where(failed_dn, 1, 0)),
        "level": np.where(up | failed_up, up_level, np.where(dn | failed_dn, dn_level, NAN)),
        "upper": ch["upper"], "lower": ch["lower"],
    }, index=df.index)
