"""The autonomous trading engine.

One cycle (`run_once`):

 1. connection check (reconnect with exponential back-off)
 2. account update -> circuit breakers (daily/weekly loss, drawdown, margin)
 3. emergency stop handling (dashboard / CLI / automatic)
 4. reconciliation: settle uncertain orders, record closed trades (exactly once)
 5. manage open positions (break-even, trailing stop, time stop, pre-news exit)
 6. for every symbol with a freshly completed bar:
        market open? -> anomaly check -> strategies -> regime/selector ->
        confidence -> conflict resolution -> risk manager -> order
 7. equity snapshot, heartbeat, daily report

The engine decides and acts on its own; it never waits for approval of an
individual trade. What it may trade *with* is bounded elsewhere: the mode
(paper/demo/live gate), the risk limits and the validation status of each
strategy. A restart resumes from persisted state and cannot duplicate orders.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from ..ai_engine import anomaly, context, performance
from ..ai_engine.explain import explain
from ..ai_engine.meta_model import MetaModel, build_features
from ..execution.broker import Broker, BrokerError
from ..execution.live_gate import LiveGate, LiveTradingLocked
from ..execution.order_manager import OrderManager
from ..execution.paper import PaperBroker
from ..execution.position_manager import plan
from ..market_data.sessions import MarketCalendar
from ..market_data.timeframes import MarketData, drop_incomplete, tf_delta
from ..monitoring import reports
from ..news_engine.monitor import NewsMonitor, NewsState
from ..notifications.notifier import Notifier
from ..risk_management.manager import RiskManager, remaining_risk
from ..strategies.base import Strategy
from ..strategies.registry import create_all
from .config import AppConfig
from .logging_setup import mask_login
from .models import ManagementPlan, Signal, SymbolSpec, Timeframe, TradingMode

log = logging.getLogger(__name__)

CONTEXT_TFS = ("M15", "H1", "H4", "D1", "W1")
LAST_BAR_KEY = "engine_last_bar"
MAX_CONSECUTIVE_ERRORS = 10


class EngineError(RuntimeError):
    pass


class TradingEngine:
    def __init__(self, config: AppConfig, broker: Broker, db, notifier: Optional[Notifier] = None,
                 news: Optional[NewsMonitor] = None, strategies: Optional[list[Strategy]] = None,
                 models_dir: Optional[str | Path] = None, live_gate: Optional[LiveGate] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = config
        self.mode = config.mode
        self.broker = broker
        self.db = db
        self.notifier = notifier or Notifier(db=db)
        self.news = news
        self.live_gate = live_gate or LiveGate(config, db)
        if strategies is None:
            # Parameters chosen by validation are the defaults; explicit config
            # overrides win, but then the strategy no longer counts as validated.
            params = {}
            for name in config.strategies.enabled:
                params[name] = {**self._validated_params(db, name), **config.strategies.params.get(name, {})}
            strategies = create_all(config.strategies.enabled, params)
        self.strategies = strategies
        ses = config.sessions
        self.calendar = MarketCalendar(ses.avoid_rollover_minutes, ses.friday_cutoff_hours_before_close,
                                       ses.monday_open_delay_minutes, ses.holidays)
        self.risk = RiskManager(config.risk, db, config.engine.day_reset_hour_utc)
        self.orders = OrderManager(broker, db, self.mode, config.execution.magic, config.execution.max_retries,
                                   config.execution.retry_delay_seconds, config.risk.max_slippage_points,
                                   sleep=sleep)
        self.symbols = list(config.symbols)
        self.specs: dict[str, SymbolSpec] = {}
        self.models: dict[str, MetaModel] = {}
        if models_dir and config.strategies.use_meta_model:
            for s in self.strategies:
                try:
                    m = MetaModel.load(models_dir, s.name)
                except Exception as exc:
                    log.error("meta model for %s not loaded: %s", s.name, exc)
                    m = None
                if m is not None and m.usable:
                    self.models[s.name] = m
        self._sleep = sleep
        self._last_bar: dict[str, str] = dict(db.get_state(LAST_BAR_KEY, {}))
        self._connected = False
        self._backoff = 1.0
        self._next_reconnect = 0.0
        self._last_snapshot: Optional[datetime] = None
        self._last_report_day = db.get_state("engine_last_report_day", "")
        self._errors = 0
        self._market_status: dict[str, str] = {}
        self._warned: set[int] = set()
        self._checked: dict[str, str] = {}
        self.running = False
        self.started = False

    # ================================================================ startup
    def start(self) -> None:
        """Connect, verify the right to trade, validate symbols, reconcile."""
        if self.mode is TradingMode.BACKTEST:
            raise EngineError("the live engine does not run in backtest mode")
        if self.mode is TradingMode.PAPER and not isinstance(self.broker, PaperBroker):
            raise EngineError("PAPER mode requires the internal simulator; refusing a real broker connection")
        if self.mode in (TradingMode.DEMO, TradingMode.LIVE) and isinstance(self.broker, PaperBroker):
            raise EngineError(f"{self.mode.value} mode requires a MetaTrader connection, not the simulator")
        if not self.symbols:
            raise EngineError("no symbols configured")
        if not self.broker.connect():
            raise EngineError("could not connect to the broker/terminal")
        self._connected = True
        account = self.broker.account_info()
        if self.mode in (TradingMode.DEMO, TradingMode.LIVE):
            self.live_gate.assert_can_trade(self.mode, account)      # raises LiveTradingLocked
        for symbol in self.symbols:
            try:
                self.specs[symbol] = self.broker.symbol_spec(symbol)
            except (BrokerError, ValueError) as exc:
                raise EngineError(f"symbol '{symbol}' is not available at the broker: {exc}") from exc
        ok, bad = self.db.verify_audit_chain()
        if not ok:
            self.notifier.notify("critical", "Audit log integrity failure", f"first bad row: {bad}")
        now = self.broker.now()
        self.orders.reconcile(now, grace=timedelta(seconds=0))
        self._sync_trades(now)
        self.db.audit("engine_start", {"mode": self.mode.value, "account": mask_login(account.login),
                                       "server": account.server, "demo_account": account.is_demo,
                                       "symbols": self.symbols, "strategies": [s.name for s in self.strategies]})
        self.started = True
        self.notifier.notify("info", f"Engine started in {self.mode.value.upper()} mode",
                             f"Account {mask_login(account.login)} ({'demo' if account.is_demo else 'REAL'}), "
                             f"equity {account.equity:.2f} {account.currency}, symbols {', '.join(self.symbols)}",
                             key="engine_start")

    # ================================================================== cycle
    def run_once(self) -> dict:
        t0 = time.perf_counter()
        out: dict = {"connected": False, "executed": 0, "rejected": 0, "closed": 0}
        if not self._ensure_connection():
            self.db.heartbeat("engine", "disconnected", {"mode": self.mode.value})
            return out
        out["connected"] = True
        now = self.broker.now()
        if isinstance(self.broker, PaperBroker):
            self.broker.sync()
        account = self.broker.account_info()

        for event, message in self.risk.update(account, now):
            level = "critical" if event in ("max_drawdown",) else "warning"
            self.notifier.notify(level, f"Risk: {event.replace('_', ' ')}", message, key=event)
            if event == "max_drawdown" and self.cfg.risk.flatten_on_emergency:
                self.orders.flatten("max_drawdown")
        self._handle_kill_switch()

        changes = self.orders.reconcile(now)
        for c in changes:
            if c["status"] == "failed":
                self._order_error(now, f"order {c['client_id']} left no trace at the broker")
        out["closed"] = len(self._sync_trades(now))
        self._manage_positions(now)

        if self.news is not None:
            self.news.refresh(now)
        for symbol in self.symbols:
            try:
                ex, rej = self._evaluate_symbol(symbol, now)
                out["executed"] += ex
                out["rejected"] += rej
            except BrokerError as exc:
                log.warning("%s: data unavailable this cycle: %s", symbol, exc)
                self._market_status[symbol] = f"data error: {exc}"
        self._housekeeping(now, time.perf_counter() - t0)
        return out

    def run_forever(self) -> None:
        if not self.started:
            self.start()
        self.running = True
        log.info("engine loop running (interval %.1fs)", self.cfg.engine.loop_interval_seconds)
        try:
            while self.running:
                try:
                    self.run_once()
                    self._errors = 0
                except LiveTradingLocked as exc:
                    self.notifier.notify("critical", "Live trading gate closed", str(exc))
                    self.running = False
                except Exception as exc:
                    self._errors += 1
                    log.exception("engine cycle failed (%d in a row)", self._errors)
                    self.db.heartbeat("engine", "error", {"error": type(exc).__name__})
                    if self._errors >= MAX_CONSECUTIVE_ERRORS and not self.risk.kill_switch().get("active"):
                        self.risk.emergency_stop(f"{self._errors} consecutive engine errors "
                                                 f"({type(exc).__name__})", actor="engine")
                        self.notifier.notify("critical", "Engine unstable - emergency stop",
                                             "Repeated internal errors. New entries are blocked.")
                self._sleep(self.cfg.engine.loop_interval_seconds)
        finally:
            self.stop()

    def stop(self) -> None:
        """Graceful stop. Open positions are left in place: each one carries
        a broker-side stop-loss and take-profit."""
        self.running = False
        try:
            self.db.heartbeat("engine", "stopped", {"mode": self.mode.value})
            self.db.audit("engine_stop", {})
        except Exception:
            log.exception("could not record engine stop")

    # ============================================================= connection
    def _ensure_connection(self) -> bool:
        try:
            alive = self.broker.is_connected()
        except Exception:
            alive = False
        if alive:
            if not self._connected:
                self._connected, self._backoff = True, 1.0
                self.db.audit("reconnected", {})
                self.notifier.notify("info", "Connection restored", "Reconciling orders and positions.")
                self.orders.reconcile(self.broker.now(), grace=timedelta(seconds=0))
            return True
        if self._connected:
            self._connected = False
            self.db.audit("connection_lost", {})
            self.notifier.notify("warning", "Connection to broker lost",
                                 "Open positions remain protected by their broker-side stops. Reconnecting.",
                                 key="connection_lost")
        wall = time.monotonic()
        if wall >= self._next_reconnect:
            try:
                ok = self.broker.connect()
            except LiveTradingLocked:
                raise
            except Exception as exc:
                log.warning("reconnect failed: %s", type(exc).__name__)
                ok = False
            if ok and self.broker.is_connected():
                return self._ensure_connection()
            self._backoff = min(self._backoff * 2.0, self.cfg.engine.reconnect_max_backoff_seconds)
            self._next_reconnect = wall + self._backoff
        return False

    # ============================================================== emergency
    def _handle_kill_switch(self) -> None:
        ks = self.risk.kill_switch()
        if not ks.get("active") or ks.get("handled"):
            return
        detail = ""
        if self.cfg.risk.flatten_on_emergency:
            res = self.orders.flatten("emergency_stop")
            detail = (f" Closed {res['closed']} position(s), cancelled {res['cancelled']} order(s)"
                      + (f", {res['failed']} FAILED to close" if res["failed"] else "") + ".")
            if res["failed"]:
                return                                   # retry next cycle; keep the flag unhandled
        self.risk.mark_kill_handled()
        self.notifier.notify("critical", "EMERGENCY STOP",
                             f"Triggered by {ks.get('actor')}: {ks.get('reason')}.{detail} "
                             "No new positions until manually resumed.")

    def _order_error(self, now: datetime, detail: str) -> None:
        msg = self.risk.on_order_error(now, detail)
        if msg:
            self.notifier.notify("critical", "Abnormal behaviour - trading suspended", msg)

    # ================================================================= trades
    def _sync_trades(self, now: datetime) -> list:
        since_raw = self.db.get_state("engine_trades_since")
        since = datetime.fromisoformat(since_raw) if since_raw else now - timedelta(days=7)
        fresh = self.orders.sync_closed_trades(since - timedelta(hours=1))
        for t in fresh:
            log.info("trade closed: %s %s %s %+.2f (%+.2fR, %s)", t.strategy, t.symbol, t.side.value, t.profit,
                     t.r_multiple, t.exit_reason)
            msg = self.risk.on_trade_closed(t, now)
            if msg:
                self.notifier.notify("warning", "Loss-streak cool-down", msg)
        for name in {t.strategy for t in fresh if t.strategy}:
            self._check_strategy_health(name)
        if fresh:
            self.db.set_state("engine_trades_since", max(t.time_close for t in fresh).isoformat())
        return fresh

    def _check_strategy_health(self, name: str) -> None:
        rows = sorted(self.db.trades(mode=self.mode.value, strategy=name), key=lambda r: r["time_close"])
        r = [x["r_multiple"] for x in rows if x["risk_amount"] > 0]
        baseline = None
        for st in self.db.strategy_statuses():
            if st["strategy"] == name and st["status"] == "validated":
                import json

                rep = json.loads(st["report"] or "{}")
                baseline = rep.get("baseline")
                break
        health = performance.evaluate(name, r, baseline)
        self.db.set_state(f"health_{name}", health.to_dict())
        if health.status == performance.DEGRADED:
            for symbol in self.symbols:
                if self.db.strategy_status(name, symbol) != "suspended":
                    self.db.set_strategy_status(name, symbol, "suspended", {"reasons": health.reasons})
            self.db.audit("strategy_suspended", {"strategy": name, "reasons": health.reasons})
            self.notifier.notify("warning", f"Strategy suspended: {name}",
                                 "Performance deteriorated:\n- " + "\n- ".join(health.reasons) +
                                 "\nRe-enable manually after review.", key=f"suspend_{name}")

    # ============================================================== positions
    def _manage_positions(self, now: datetime) -> None:
        magic = self.cfg.execution.magic
        for p in self.broker.positions():
            if p.magic != magic:
                continue
            info = self.orders.management_for(p)
            spec = self.specs.get(p.symbol)
            if info is None or spec is None:
                if p.ticket not in self._warned:
                    self._warned.add(p.ticket)
                    self.notifier.notify("warning", "Unknown position",
                                         f"Position {p.ticket} on {p.symbol} carries this system's magic number "
                                         "but has no order record. It is left untouched.")
                continue
            if p.sl <= 0:
                # A position without a stop is never tolerated: restore the original stop.
                if self.orders.modify_stop(p, info["sl_initial"], "restore_missing_stop"):
                    self.notifier.notify("critical", "Missing stop-loss restored",
                                         f"{p.symbol} position {p.ticket} had no stop.")
                else:
                    self.orders.close(p, "no_stop")
                continue
            if self.news is not None:
                st = self.news.state({spec.currency_base, spec.currency_profit}, now)
                if st.close_positions:
                    self.orders.close(p, "pre_news_exit")
                    continue
            mgmt = ManagementPlan.from_dict(info["management"])
            tf = info["timeframe"] or "M15"
            bars = int((now - p.time_open) / tf_delta(tf).to_pytimedelta())
            price = p.price_current or p.price_open
            act = plan(p.side.sign, p.price_open, p.sl, info["sl_initial"], price, mgmt, bars, spec.point,
                       min_step=0.1 * mgmt.atr)
            if act.close:
                self.orders.close(p, act.reason)
            elif act.new_sl is not None:
                self.orders.modify_stop(p, round(act.new_sl, spec.digits), act.reason)

    # ================================================================ signals
    @staticmethod
    def _validated_params(db, name: str, symbol: Optional[str] = None) -> dict:
        """Parameters recorded by the validation run that produced a
        VALIDATED status (empty when there is none)."""
        import json

        for st in db.strategy_statuses():
            if st["strategy"] == name and st["status"] == "validated" and (symbol is None or st["symbol"] == symbol):
                return dict(json.loads(st["report"] or "{}").get("params") or {})
        return {}

    def _strategy_allowed(self, s: Strategy, symbol: str) -> tuple[bool, str]:
        status = self.db.strategy_status(s.name, symbol)
        if status == "suspended":
            return False, "strategy suspended (performance deterioration)"
        if status == "validated":
            # The verdict belongs to one exact parameter set.
            validated = self._validated_params(self.db, s.name, symbol)
            changed = sorted(k for k, v in validated.items() if k in s.params and s.params[k] != v)
            if not changed:
                return True, ""
            status = f"validated with different parameters ({', '.join(changed)})"
        if self.mode is TradingMode.PAPER and self.cfg.strategies.allow_unvalidated_in_paper:
            return True, ""
        return False, f"strategy is '{status}' for {symbol}: only VALIDATED strategies trade in {self.mode.value}"

    def _due(self, symbol: str, now: datetime) -> list[Strategy]:
        due = []
        for s in self.strategies:
            step = tf_delta(s.entry_tf)
            expected_open = (pd.Timestamp(now).floor(step) - step).isoformat()
            key = f"{symbol}|{s.name}"
            if self._last_bar.get(key) != expected_open and self._checked.get(key) != expected_open:
                due.append(s)
        return due

    def _load(self, symbol: str, tfs: set[str], now: datetime) -> MarketData:
        frames = {}
        stamp = pd.Timestamp(now)
        min_tf = getattr(self.broker, "data", None)
        floor = Timeframe(getattr(min_tf, "min_timeframe", "M1")).minutes if min_tf is not None else 1
        for tf in sorted(tfs, key=lambda x: Timeframe(x).minutes):
            if Timeframe(tf).minutes < floor:
                continue
            frame = self.broker.rates(symbol, tf, self.cfg.engine.history_bars)
            if getattr(frame.index, "unit", "ns") != "ns":
                # Brokers deliver second-resolution timestamps; keep every
                # timeframe on one resolution so alignment is exact.
                frame = frame.copy()
                frame.index = frame.index.as_unit("ns")
            frames[tf] = drop_incomplete(frame, tf, stamp)
        return MarketData(symbol, self.specs[symbol], frames)

    def _evaluate_symbol(self, symbol: str, now: datetime) -> tuple[int, int]:
        spec = self.specs[symbol]
        if not self.calendar.is_open(now, spec.asset_class):
            self._market_status[symbol] = "market closed"
            return 0, 0
        due = self._due(symbol, now)
        if not due:
            return 0, 0
        tick = self.broker.tick(symbol)
        entry_ok, entry_why = self.calendar.entry_allowed(now, spec.asset_class)
        self._market_status[symbol] = "monitoring" if entry_ok else f"no entries: {entry_why}"
        tfs = set(CONTEXT_TFS)
        for s in due:
            tfs.update(s.timeframes)
        data = self._load(symbol, tfs, now)
        news_state = self.news.state({spec.currency_base, spec.currency_profit}, now) if self.news else NewsState()

        candidates = []
        marks: dict[str, str] = {}
        for s in due:
            if not data.has(s.entry_tf) or any(not data.has(tf) for tf in s.context_tfs):
                continue
            frame = data.frame(s.entry_tf)
            if len(frame) <= s.warmup_bars:
                continue
            last_open = frame.index[-1]
            key = f"{symbol}|{s.name}"
            step = tf_delta(s.entry_tf)
            self._checked[key] = (pd.Timestamp(now).floor(step) - step).isoformat()
            if self._last_bar.get(key) == last_open.isoformat():
                continue
            marks[key] = last_open.isoformat()
            close_time = last_open + tf_delta(s.entry_tf)
            age = (pd.Timestamp(now) - close_time).total_seconds()
            if age > max(90.0, 0.25 * Timeframe(s.entry_tf).minutes * 60.0):
                continue                                  # stale bar (e.g. just restarted): never chase it
            sig = s.generate(data)
            row = sig.iloc[-1]
            if int(row.side) == 0:
                continue
            signal = s.to_signal(symbol, last_open, row, spec.digits)
            ctx = context.build_context(data, s.entry_tf)
            atr = float(row.atr)
            meta_prob = None
            model = self.models.get(s.name)
            if model is not None:
                try:
                    meta_prob = model.predict(build_features(sig.iloc[-1:], ctx.iloc[-1:]))
                except Exception:
                    log.exception("meta model prediction failed for %s", s.name)
            sel = context.select(s, sig.iloc[-1:], ctx.iloc[-1:], self.cfg.strategies.min_confidence,
                                 spread_atr_ratio=tick.spread / atr if atr > 0 else 1.0,
                                 news_factor=1.0 if news_state.risk_factor >= 0.999 else 0.5,
                                 meta_prob=meta_prob).iloc[-1]
            crow = ctx.iloc[-1]
            comps = {k: float(sel[k]) for k in ("c_strength", "c_regime", "c_timeframes", "c_cost", "c_news")}
            if meta_prob is not None:
                comps["meta_prob"] = float(meta_prob[-1])
            cinfo = {"adx_h1": _num(crow.get("adx_h1")), "er_h1": _num(crow.get("er_h1")),
                     "vol_rank": _num(crow.get("vol_rank")),
                     "trends": {tf: float(crow.get(f"trend_{tf}", 0.0)) for tf in ("H1", "H4", "D1", "W1")}}
            reasons: list[str] = []
            allowed, why = self._strategy_allowed(s, symbol)
            if not allowed:
                reasons.append(why)
            if sel["reject_reason"]:
                reasons.append(str(sel["reject_reason"]))
            if not entry_ok:
                reasons.append(entry_why)
            if news_state.blocked:
                reasons.append(news_state.reason)
            rep = anomaly.check_market(frame, tick, spec, now, self.cfg.costs.spread_points.get(symbol),
                                       max_tick_age=self.cfg.sessions.max_tick_age_seconds)
            reasons.extend(rep.flags)
            candidates.append({"strategy": s, "signal": signal, "confidence": float(sel["confidence"]),
                               "regime": str(sel["regime"]), "components": comps, "context": cinfo,
                               "reasons": reasons, "atr": atr})
        if marks:
            self._last_bar.update(marks)
            self.db.set_state(LAST_BAR_KEY, self._last_bar)
        if not candidates:
            return 0, 0

        # Conflicting signals: opposite directions on the same symbol in the
        # same cycle. Keep the clearly stronger one, otherwise trade neither.
        viable = [c for c in candidates if not c["reasons"]]
        keep = context.resolve_conflicts([c["signal"].side.sign for c in viable],
                                         [c["confidence"] for c in viable])
        for c, k in zip(viable, keep):
            if not k:
                c["reasons"].append("conflicting signals across strategies")

        executed = rejected = 0
        news_text = news_state.describe(now) if self.news else None
        for c in sorted(candidates, key=lambda c: c["confidence"], reverse=True):
            signal: Signal = c["signal"]
            risk_info = None
            status = "rejected"
            if not c["reasons"]:
                account = self.broker.account_info()
                positions = self.broker.positions()
                for p in positions:
                    if p.symbol not in self.specs:
                        try:
                            self.specs[p.symbol] = self.broker.symbol_spec(p.symbol)
                        except (BrokerError, ValueError):
                            pass
                scale, scale_reason = self.risk.risk_scale(account.equity, c["confidence"],
                                                           self.cfg.strategies.min_confidence,
                                                           news_state.risk_factor)
                day_start = (now - timedelta(hours=self.cfg.engine.day_reset_hour_utc)).replace(
                    hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=self.cfg.engine.day_reset_hour_utc)
                fresh_tick = self.broker.tick(symbol)
                decision = self.risk.evaluate(signal, account, positions, fresh_tick, spec, self.specs, now,
                                              c["atr"], self.db.count_orders_since(day_start), scale, scale_reason)
                risk_info = decision.to_dict()
                if decision.approved:
                    status, result, cid = self.orders.submit(signal, decision)
                    if status in ("filled", "placed"):
                        executed += 1
                        slip = abs((result.fill_price or decision.entry_price) - decision.entry_price) / spec.point
                        if status == "filled" and slip > self.cfg.risk.max_slippage_points:
                            self.notifier.notify(
                                "warning", f"Abnormal slippage on {symbol}",
                                f"Filled {slip:.0f} points from the expected price "
                                f"(tolerance {self.cfg.risk.max_slippage_points}).", key=f"slip_{cid}")
                            self._order_error(now, f"{symbol} slippage {slip:.0f} points")
                    elif status == "duplicate":
                        continue
                    else:
                        c["reasons"].append(f"order {status}: {result.message if result else ''}")
                        if status in ("rejected", "unknown"):
                            self._order_error(now, f"{symbol} {status}")
                else:
                    c["reasons"].extend(decision.reasons)
            action = "executed" if status in ("filled", "placed") else "rejected"
            if action == "rejected":
                rejected += 1
            text = explain(signal, action=action, regime=c["regime"], components=c["components"],
                           confidence=c["confidence"], min_confidence=self.cfg.strategies.min_confidence,
                           risk=risk_info, rejections=c["reasons"] or None, context=c["context"],
                           news=news_text, digits=spec.digits)
            self.db.record_decision(time=now, symbol=symbol, strategy=signal.strategy, side=signal.side.value,
                                    action=action, confidence=c["confidence"], regime=c["regime"],
                                    explanation=text,
                                    details={"components": c["components"], "risk": risk_info,
                                             "reasons": c["reasons"], "sl": signal.sl, "tp": signal.tp})
            if action == "executed":
                log.info("%s", text.replace("\n", " | "))
                self.notifier.notify("info", f"Trade opened: {signal.side.value.upper()} {symbol}", text,
                                     key=f"open_{symbol}_{signal.strategy}_{signal.time.isoformat()}")
        return executed, rejected

    # =========================================================== housekeeping
    def _housekeeping(self, now: datetime, cycle_seconds: float) -> None:
        account = self.broker.account_info()
        positions = self.broker.positions()
        open_risk = 0.0
        for p in positions:
            spec = self.specs.get(p.symbol)
            if spec is not None and p.sl > 0:
                open_risk += remaining_risk(p, spec)
        every = timedelta(seconds=self.cfg.engine.equity_snapshot_seconds)
        if self._last_snapshot is None or now - self._last_snapshot >= every:
            self.db.snapshot_equity(time=now, mode=self.mode.value, balance=account.balance,
                                    equity=account.equity, margin=account.margin,
                                    margin_free=account.margin_free, open_positions=len(positions),
                                    open_risk=open_risk)
            self._last_snapshot = now
        can_open, blocks = self.risk.can_open(now)
        rows = []
        for p in positions:
            info = self.orders.management_for(p) if p.magic == self.cfg.execution.magic else None
            spec = self.specs.get(p.symbol)
            rows.append({"ticket": p.ticket, "symbol": p.symbol, "side": p.side.value, "volume": p.volume,
                         "price_open": p.price_open, "price_current": p.price_current, "sl": p.sl, "tp": p.tp,
                         "profit": p.profit, "swap": p.swap, "time_open": p.time_open.isoformat(),
                         "strategy": info["strategy"] if info else "(external)",
                         "risk": remaining_risk(p, spec) if spec is not None and p.sl > 0 else None,
                         "digits": spec.digits if spec is not None else 5})
        self.db.set_state("positions", rows)
        self.db.heartbeat("engine", "running", {
            "mode": self.mode.value, "engine_time": now.isoformat(), "cycle_ms": round(cycle_seconds * 1000, 1),
            "positions": len(positions), "open_risk": round(open_risk, 2), "can_open": can_open,
            "blocks": blocks, "markets": dict(self._market_status),
            "sessions": self.calendar.active_sessions(now),
            "account": {**account.to_dict(), "login": mask_login(account.login)},
            "synthetic_data": bool(getattr(getattr(self.broker, "data", None), "synthetic", False)),
            "broker": self.broker.name,
        })
        day = now.strftime("%Y-%m-%d")
        if now.hour >= self.cfg.engine.daily_report_hour_utc and self._last_report_day != day:
            self._last_report_day = day
            self.db.set_state("engine_last_report_day", day)
            text = reports.daily_report(self.db, self.mode.value, self.risk.snapshot(account.equity),
                                        {**account.to_dict()}, now)
            try:
                reports.write_report(text, self.cfg.resolve_path("reports"), now)
            except OSError:
                log.exception("could not write the daily report")
            self.notifier.notify("info", "Daily report", text, key=f"daily_{day}")


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None
