"""Event-driven backtester.

It reuses the production components instead of re-implementing them:

* signals        -> the strategies' own ``generate`` (causal, vectorised)
* selection      -> ``ai_engine.context.select`` (regime + confidence)
* risk           -> the real ``RiskManager`` (sizing, limits, circuit breakers)
* fills & costs  -> ``PaperBroker`` + ``CostModel`` (spread, commission, swap,
                    adverse slippage, execution delay, gap-through stops)
* management     -> ``position_manager.plan`` (break-even, trailing, time stop)

Timing: a signal produced at the close of bar *t* is executed at the OPEN of
the next base-timeframe bar, at the ask/bid plus slippage. Stops and targets
are then resolved bar by bar on the base timeframe (stop first when both are
inside one bar). Nothing is ever filled at a price that was not tradable
after the decision was made.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from ..ai_engine import context
from ..ai_engine.meta_model import FEATURES, build_features
from ..core.models import (UTC, ClosedTrade, ManagementPlan, OrderRequest, Side, Signal, Tick,
                           Timeframe)
from ..execution.paper import PaperBroker
from ..execution.position_manager import plan
from ..market_data.sessions import MarketCalendar
from ..market_data.timeframes import MarketData, tf_delta
from ..news_engine.events import EconomicEvent
from ..news_engine.monitor import blackout_mask
from ..risk_management.limits import RiskLimits
from ..risk_management.manager import RiskManager
from ..strategies.base import Strategy
from .costs import CostModel


class MemoryStore:
    """Minimal in-memory stand-in for the database (risk state only)."""

    def __init__(self) -> None:
        self._kv: dict = {}

    def get_state(self, key, default=None):
        return self._kv.get(key, default)

    def set_state(self, key, value) -> None:
        self._kv[key] = value

    def audit(self, *a, **k) -> str:
        return ""


@dataclass
class BacktestSettings:
    initial_balance: float = 10_000.0
    currency: str = "USD"
    leverage: int = 30
    limits: RiskLimits = field(default_factory=RiskLimits)
    min_confidence: float = 0.55
    use_selector: bool = True              # regime filter + confidence threshold
    stop_on_drawdown: bool = True          # honour the max-drawdown suspension
    calendar: MarketCalendar = field(default_factory=MarketCalendar)
    news_events: Optional[list[EconomicEvent]] = None
    news_block_before: int = 30
    news_block_after: int = 30
    day_reset_hour_utc: int = 0


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity: pd.Series                      # equity at bar closes (irregular index)
    initial_balance: float
    start: Optional[pd.Timestamp]
    end: Optional[pd.Timestamp]
    signals_total: int = 0
    signals_taken: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    synthetic: bool = False
    bars_in_market: int = 0
    bars_total: int = 0

    @property
    def r_multiples(self) -> np.ndarray:
        return np.array([t.r_multiple for t in self.trades if t.risk_amount > 0], dtype=float)


class Backtester:
    def __init__(self, data: dict[str, MarketData], base_tf: str, strategies: list[Strategy], cost: CostModel,
                 settings: Optional[BacktestSettings] = None) -> None:
        self.data = data
        self.base_tf = base_tf
        self.strategies = strategies
        self.cost = cost
        self.settings = settings or BacktestSettings()
        self._events: Optional[pd.DataFrame] = None
        self._features: Optional[np.ndarray] = None
        self.synthetic = any(md.frame(base_tf).attrs.get("synthetic") for md in data.values())
        for s in strategies:
            if Timeframe(s.entry_tf).minutes < Timeframe(base_tf).minutes:
                raise ValueError(f"{s.name} needs {s.entry_tf} bars but the base data is {base_tf}")

    # ---------------------------------------------------------------- signals
    def signal_events(self) -> pd.DataFrame:
        """Every strategy signal over the whole history with its selection
        verdict. Computed once; windows are sliced out of it (valid because
        signal generation is causal)."""
        if self._events is not None:
            return self._events
        st = self.settings
        rows, feats = [], []
        for symbol, md in self.data.items():
            spec = md.spec
            currencies = {spec.currency_base, spec.currency_profit}
            typical = self.cost.typical_spread_points(symbol) * spec.point
            for s in self.strategies:
                sig = s.generate(md)
                live = sig["side"].values != 0
                if not live.any():
                    continue
                ctx = context.build_context(md, s.entry_tf)
                exec_time = sig.index + tf_delta(s.entry_tf)
                frame = md.frame(s.entry_tf)
                spread = frame["spread"].values * spec.point if "spread" in frame.columns else np.full(
                    len(frame), typical)
                with np.errstate(invalid="ignore", divide="ignore"):
                    ratio = np.where(sig["atr"].values > 0, spread / sig["atr"].values, 1.0)
                news = None
                if st.news_events:
                    blocked = blackout_mask(exec_time, st.news_events, currencies, st.news_block_before,
                                            st.news_block_after)
                    news = np.where(blocked, 0.0, 1.0)
                sel = context.select(s, sig, ctx, st.min_confidence if st.use_selector else 0.0,
                                     spread_atr_ratio=ratio, news_factor=news)
                reason = sel["reject_reason"].values.copy()
                if not st.use_selector:
                    reason[:] = ""
                session_ok = st.calendar.entry_mask(exec_time, spec.asset_class)
                reason[live & (reason == "") & ~session_ok] = "session rules (closed/rollover/weekly edge)"
                if news is not None:
                    reason[live & (reason == "") & (news == 0.0)] = "high-impact news window"
                f = build_features(sig, ctx)
                idx = np.flatnonzero(live)
                mgmt = s.management(0.0)
                for j, i in enumerate(idx):
                    rows.append((exec_time[i], symbol, s.name, s.entry_tf, int(sig["side"].values[i]),
                                 float(sig["sl"].values[i]), float(sig["tp"].values[i]),
                                 float(sig["close"].values[i]), float(sig["atr"].values[i]),
                                 float(sig["strength"].values[i]), float(sel["confidence"].values[i]),
                                 str(sel["regime"].values[i]), str(reason[i]), mgmt.breakeven_at_r,
                                 mgmt.trail_atr_mult, mgmt.trail_start_r, mgmt.max_bars))
                    feats.append(f.values[j])
        cols = ["exec_time", "symbol", "strategy", "tf", "side", "sl", "tp", "ref", "atr", "strength",
                "confidence", "regime", "reject", "be_r", "trail", "trail_start", "max_bars"]
        ev = pd.DataFrame(rows, columns=cols)
        order = np.argsort(ev["exec_time"].values, kind="stable") if len(ev) else np.array([], dtype=int)
        self._events = ev.iloc[order].reset_index(drop=True)
        self._features = np.array(feats, dtype=float)[order] if len(feats) else np.zeros((0, len(FEATURES)))
        return self._events

    def features_for(self, event_ids: list[int]) -> pd.DataFrame:
        self.signal_events()
        return pd.DataFrame(self._features[event_ids], columns=FEATURES)

    # -------------------------------------------------------------------- run
    def run(self, start: Optional[pd.Timestamp] = None, end: Optional[pd.Timestamp] = None,
            seed: Optional[int] = None, cost: Optional[CostModel] = None) -> BacktestResult:
        st = self.settings
        cost = cost or self.cost
        if seed is not None:
            cost.reseed(seed)
        else:
            cost.reseed(cost.seed)
        events = self.signal_events()
        base_step = tf_delta(self.base_tf)
        step_ns = base_step.value
        bar_seconds = base_step.total_seconds()

        # ---- per-symbol arrays --------------------------------------------
        arrays = {}
        all_times = []
        for symbol, md in self.data.items():
            f = md.frame(self.base_tf)
            if start is not None:
                f = f[f.index >= start]
            if end is not None:
                f = f[f.index < end]
            spec = md.spec
            typical = self.cost.typical_spread_points(symbol)
            sp_pts = f["spread"].values if "spread" in f.columns else np.full(len(f), np.nan)
            spread = np.array([cost.spread_price(symbol, spec.point, x) for x in sp_pts]) if len(f) else np.array([])
            t_ns = f.index.values.astype("datetime64[ns]").astype(np.int64)
            arrays[symbol] = (t_ns, f["open"].values, f["high"].values, f["low"].values, f["close"].values, spread,
                              spec, typical)
            all_times.append(t_ns)
        times = np.unique(np.concatenate(all_times)) if all_times else np.array([], dtype=np.int64)
        result = BacktestResult([], pd.Series(dtype=float), st.initial_balance,
                                pd.Timestamp(times[0], tz="UTC") if len(times) else None,
                                pd.Timestamp(times[-1], tz="UTC") if len(times) else None,
                                synthetic=self.synthetic, bars_total=len(times))
        if not len(times):
            return result

        broker = PaperBroker(balance=st.initial_balance, currency=st.currency, leverage=st.leverage, cost=cost,
                             specs={s: md.spec for s, md in self.data.items()})
        for symbol in self.data:
            broker._generic.add(symbol)            # refresh tick values from simulated prices
        risk = RiskManager(st.limits, MemoryStore(), st.day_reset_hour_utc, quiet=True)
        specs = {s: md.spec for s, md in self.data.items()}

        ev_ns = events["exec_time"].values.astype("datetime64[ns]").astype(np.int64) if len(events) else np.array(
            [], dtype=np.int64)
        ev_rows = events.to_dict("records")
        lo = int(np.searchsorted(ev_ns, times[0], "left"))
        hi = int(np.searchsorted(ev_ns, times[-1], "right"))
        ev_ptr = lo
        result.signals_total = hi - lo

        meta: dict[int, dict] = {}                 # position ticket -> management info
        eq_t: list[int] = [int(times[0])]
        eq_v: list[float] = [st.initial_balance]
        day_trades: dict[str, int] = {}
        n_closed = 0
        halted = False
        i, n = 0, len(times)

        def reject(why: str) -> None:
            result.rejections[why] = result.rejections.get(why, 0) + 1

        while i < n:
            t = int(times[i])
            if not broker._positions and not broker._pending:
                if halted or ev_ptr >= hi:
                    break
                if ev_ns[ev_ptr] > t:                # nothing can happen until the next signal
                    i = int(np.searchsorted(times, ev_ns[ev_ptr], "left"))
                    if i >= n:
                        break
                    t = int(times[i])
            now = datetime.fromtimestamp(t / 1e9, UTC)
            now_close = datetime.fromtimestamp((t + step_ns) / 1e9, UTC)
            in_market = bool(broker._positions)

            # 1. open of the bar: update prices (gap-through stops trigger here)
            present = []
            for symbol, (t_ns, o, h, l, c, spread, spec, _) in arrays.items():
                k = int(np.searchsorted(t_ns, t, "left"))
                if k < len(t_ns) and t_ns[k] == t:
                    present.append((symbol, k))
                    if broker._positions or broker._pending:
                        broker.set_price(symbol, o[k], o[k] + spread[k], now)
                    else:
                        broker._px[symbol] = (o[k], o[k] + spread[k], now)

            # 2. execute signals decided at (or just before) this open
            if ev_ptr < hi and ev_ns[ev_ptr] <= t:
                batch = []
                while ev_ptr < hi and ev_ns[ev_ptr] <= t:
                    batch.append(ev_ptr)
                    ev_ptr += 1
                account = broker.account_info()
                for event_name, _ in risk.update(account, now, persist=False):
                    if event_name == "max_drawdown":
                        result.flags.append(f"max drawdown limit hit at {now:%Y-%m-%d}")
                        halted = st.stop_on_drawdown
                cands = []
                for e in batch:
                    row = ev_rows[e]
                    if row["reject"]:
                        reject(row["reject"])
                    elif t - ev_ns[e] > 2 * step_ns:
                        reject("stale signal (market gap)")
                    elif not any(sym == row["symbol"] for sym, _ in present):
                        reject("no quote at execution time")
                    else:
                        cands.append(e)
                by_symbol: dict[str, list[int]] = {}
                for e in cands:
                    by_symbol.setdefault(ev_rows[e]["symbol"], []).append(e)
                final = []
                for symbol, es in by_symbol.items():
                    keep = context.resolve_conflicts([ev_rows[e]["side"] for e in es],
                                                     [ev_rows[e]["confidence"] for e in es])
                    for e, k_ in zip(es, keep):
                        if k_:
                            final.append(e)
                        else:
                            reject("conflicting signals across strategies")
                for e in sorted(final, key=lambda x: ev_rows[x]["confidence"], reverse=True):
                    if halted:
                        reject("trading suspended (max drawdown)")
                        continue
                    row = ev_rows[e]
                    symbol = row["symbol"]
                    spec = specs[symbol]
                    bid, ask, _ = broker._px[symbol]
                    side = Side.BUY if row["side"] > 0 else Side.SELL
                    signal = Signal(strategy=row["strategy"], symbol=symbol, timeframe=row["tf"], time=now,
                                    side=side, sl=round(row["sl"], spec.digits), tp=round(row["tp"], spec.digits),
                                    ref_price=row["ref"], strength=row["strength"])
                    account = broker.account_info()
                    scale, why = risk.risk_scale(account.equity, row["confidence"], st.min_confidence)
                    day = risk._day_key(now)
                    decision = risk.evaluate(signal, account, broker.positions(), Tick(symbol, now, bid, ask), spec,
                                             specs, now, row["atr"], day_trades.get(day, 0), scale, why)
                    if not decision.approved:
                        reject(decision.reasons[0] if decision.reasons else "risk")
                        continue
                    sym_arr = arrays[symbol]
                    k = next(kk for sym, kk in present if sym == symbol)
                    broker.set_fill_context(symbol, sym_arr[2][k - 1] - sym_arr[3][k - 1] if k > 0 else 0.0,
                                            bar_seconds)
                    res = broker.send_order(OrderRequest(
                        client_id=str(e), symbol=symbol, side=side, volume=decision.volume, sl=signal.sl,
                        tp=signal.tp, deviation_points=st.limits.max_slippage_points, strategy=row["strategy"],
                        comment=str(e)))
                    if not res.ok:
                        reject(f"order rejected: {res.message}")
                        continue
                    result.signals_taken += 1
                    day_trades[day] = day_trades.get(day, 0) + 1
                    meta[res.position_ticket] = {
                        "sl_initial": signal.sl, "tf_ns": tf_delta(row["tf"]).value, "open_ns": t,
                        "plan": ManagementPlan(breakeven_at_r=_opt(row["be_r"]), trail_atr_mult=_opt(row["trail"]),
                                               trail_start_r=float(row["trail_start"]),
                                               max_bars=None if _opt(row["max_bars"]) is None else int(row["max_bars"]),
                                               atr=float(row["atr"]))}

            # 3. the bar itself, then position management at its close
            if broker._positions or broker._pending:
                for symbol, k in present:
                    t_ns, o, h, l, c, spread, spec, _ = arrays[symbol]
                    broker.set_fill_context(symbol, h[k] - l[k], bar_seconds)
                    broker.process_bar(symbol, now, o[k], h[k], l[k], c[k], spread[k], now_close)
                for p in list(broker._positions.values()):
                    info = meta.get(p.ticket)
                    if info is None or p.symbol not in broker._px:
                        continue
                    bid, ask, when = broker._px[p.symbol]
                    if when != now_close:
                        continue                         # no bar for this symbol at this time
                    spec = specs[p.symbol]
                    price = bid if p.side == "buy" else ask
                    bars = int((t + step_ns - info["open_ns"]) // info["tf_ns"])
                    act = plan(p.sign, p.price_open, p.sl, info["sl_initial"], price, info["plan"], bars,
                               spec.point, min_step=0.1 * info["plan"].atr)
                    if act.close:
                        broker.set_fill_context(p.symbol, 0.0, bar_seconds)
                        broker.close_position(p.ticket, reason=act.reason)
                    elif act.new_sl is not None:
                        broker.modify_position(p.ticket, round(act.new_sl, spec.digits), p.tp)
            else:
                for symbol, k in present:
                    t_ns, o, h, l, c, spread, spec, _ = arrays[symbol]
                    broker._px[symbol] = (c[k], c[k] + spread[k], now_close)

            # 4. bookkeeping
            closed = broker.all_closed_trades
            if len(closed) > n_closed:
                for tr in closed[n_closed:]:
                    risk.on_trade_closed(tr, now_close)
                    meta.pop(tr.position_ticket, None)
                n_closed = len(closed)
            if in_market or broker._positions:
                result.bars_in_market += 1
                account = broker.account_info()
                eq_t.append(t + step_ns)
                eq_v.append(account.equity)
                for event_name, _ in risk.update(account, now_close, persist=False):
                    if event_name == "max_drawdown":
                        result.flags.append(f"max drawdown limit hit at {now_close:%Y-%m-%d}")
                        if st.stop_on_drawdown:
                            halted = True
                            for p in list(broker._positions.values()):
                                broker.close_position(p.ticket, reason="max_drawdown")
                            for tr in broker.all_closed_trades[n_closed:]:
                                meta.pop(tr.position_ticket, None)
                            n_closed = len(broker.all_closed_trades)
                            eq_t.append(t + step_ns + 1)
                            eq_v.append(broker.account_info().equity)
            i += 1

        # close whatever is still open at the last known price
        for p in list(broker._positions.values()):
            broker.set_fill_context(p.symbol, 0.0, bar_seconds)
            broker.close_position(p.ticket, reason="end_of_test")
        final_equity = broker.account_info().equity
        eq_t.append(int(times[-1]) + step_ns + 2)
        eq_v.append(final_equity)
        idx = pd.to_datetime(np.array(eq_t, dtype=np.int64), utc=True)
        equity = pd.Series(eq_v, index=idx)
        result.equity = equity[~equity.index.duplicated(keep="last")]
        result.trades = list(broker.all_closed_trades)
        if halted:
            result.flags.append("trading suspended for the rest of the test (max drawdown)")
        return result


def _opt(x):
    return None if x is None or (isinstance(x, float) and x != x) else x

