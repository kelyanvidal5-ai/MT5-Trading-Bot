"""End-to-end tests of the autonomous engine on a replayed (synthetic) feed.

The feed is stepped bar by bar; the engine runs one cycle per step. These
tests exercise the whole chain: data -> strategies -> selection -> risk ->
orders -> position management -> persistence, plus restart safety, the
emergency stop and the mode gates.
"""
from datetime import timedelta

import pandas as pd
import pytest

from mt5quant.backtesting.costs import CostModel
from mt5quant.core.config import load_config
from mt5quant.core.engine import EngineError, TradingEngine
from mt5quant.core.models import TradingMode
from mt5quant.database.db import Database
from mt5quant.execution.live_gate import LiveGate, LiveTradingLocked
from mt5quant.execution.mt5_broker import MT5Broker
from mt5quant.execution.paper import PaperBroker
from mt5quant.market_data.feeds import ReplayFeed
from mt5quant.monitoring import reports, stats
from mt5quant.news_engine.monitor import NewsState
from mt5quant.notifications.notifier import Channel, Notifier
from mt5quant.strategies.registry import create_all
from tests.fake_mt5 import FakeMT5

START = 22_000


class Capture(Channel):
    name = "capture"

    def __init__(self):
        self.sent = []

    def send(self, level, title, body):
        self.sent.append((level, title, body))


ALL3 = ("scalp_pullback", "liquidity_sweep", "intraday_vwap")
M15 = ("liquidity_sweep", "intraday_vwap")          # cheaper: evaluated every third M5 step


def build(bars, db, start=START, strategies=M15, overrides=None,
          news=None, state=None):
    cfg = load_config(use_local=False, overrides={
        "symbols": ["EURUSD"], "strategies": {"enabled": list(strategies), "min_confidence": 0.5},
        "news": {"enabled": False}, **(overrides or {})})
    feed = ReplayFeed({"EURUSD": bars}, base_tf="M5", start=bars.index[start], spread_points={"EURUSD": 8})
    broker = PaperBroker(data=feed, balance=10_000, cost=CostModel(slippage_points_mean=1.0, execution_delay_ms=0),
                         on_change=lambda s: db.set_state("paper_broker", s))
    broker.load_state(state if state is not None else db.get_state("paper_broker"))
    cap = Capture()
    engine = TradingEngine(cfg, broker, db, notifier=Notifier([cap], db=db, min_interval_seconds=0), news=news,
                           strategies=create_all(list(strategies)), sleep=lambda s: None)
    return engine, feed, broker, cap


def run(engine, feed, steps):
    totals = {"executed": 0, "rejected": 0, "closed": 0}
    for _ in range(steps):
        feed.advance(1)
        out = engine.run_once()
        for k in totals:
            totals[k] += out[k]
    return totals


@pytest.fixture(scope="module")
def session(bars_m5):
    """One longer run shared by several read-only assertions."""
    db = Database(":memory:")
    engine, feed, broker, cap = build(bars_m5, db, strategies=ALL3)
    engine.start()
    totals = run(engine, feed, 1500)
    return {"db": db, "engine": engine, "feed": feed, "broker": broker, "cap": cap, "totals": totals}


def test_engine_trades_autonomously_and_records_everything(session):
    db, totals = session["db"], session["totals"]
    assert totals["executed"] >= 3, "expected the engine to open trades on its own"
    orders = db.query("SELECT * FROM orders")
    assert len([o for o in orders if o["status"] == "filled"]) == totals["executed"]
    decisions = db.decisions(1000)
    assert {d["action"] for d in decisions} <= {"executed", "rejected"}
    executed = [d for d in decisions if d["action"] == "executed"]
    assert len(executed) == totals["executed"]
    for d in executed:
        assert "Hypothesis:" in d["explanation"] and "Confidence" in d["explanation"]
        assert "Risk:" in d["explanation"] and d["confidence"] >= 0.5
    for d in [x for x in decisions if x["action"] == "rejected"]:
        assert "Not traded because:" in d["explanation"]
    assert db.verify_audit_chain() == (True, 0)


def test_every_order_has_a_stop_and_respects_the_risk_budget(session):
    db = session["db"]
    for o in db.query("SELECT * FROM orders WHERE status='filled'"):
        assert o["sl"] > 0 and o["tp"] > 0
        assert (o["fill_price"] - o["sl"]) * (1 if o["side"] == "buy" else -1) > 0
        assert o["risk_amount"] <= 10_500 * 0.0025 + 1e-6          # <= 0.25% of (generous) equity
    snaps = db.equity_curve()
    assert max(s["open_positions"] for s in snaps) <= 3
    assert max(s["open_risk"] / s["equity"] for s in snaps) <= 0.0075 + 1e-9


def test_closed_trades_are_recorded_once_with_attribution(session):
    db, broker = session["db"], session["broker"]
    trades = db.trades(mode="paper")
    assert len(trades) == len(broker.all_closed_trades) == session["totals"]["closed"]
    assert len(trades) >= 2
    for t in trades:
        assert t["strategy"] in {"scalp_pullback", "liquidity_sweep", "intraday_vwap"}
        assert t["risk_amount"] > 0 and t["client_id"]
        assert t["exit_reason"] in {"sl", "tp", "time_exit", "breakeven", "trailing", "manual"}
    account = broker.account_info()
    realised = sum(t["profit"] for t in trades)
    open_costs = sum(p.commission for p in broker.positions())
    assert account.balance == pytest.approx(10_000 + realised + open_costs, abs=0.05)


def test_no_trades_outside_allowed_sessions(session):
    engine, db = session["engine"], session["db"]
    for o in db.query("SELECT * FROM orders WHERE status='filled'"):
        when = pd.Timestamp(o["signal_time"]).to_pydatetime()
        assert engine.calendar.entry_allowed(when)[0], f"order at {when} violates the session rules"


def test_heartbeat_snapshots_and_reports(session):
    db, engine, broker = session["db"], session["engine"], session["broker"]
    beat = db.heartbeats()["engine"]
    assert beat["status"] == "running" and beat["details"]["mode"] == "paper"
    assert beat["details"]["synthetic_data"] is True and beat["details"]["account"]["login"] != ""
    assert reports.health(db)["alive"] is True
    assert len(db.equity_curve()) > 100
    now = broker.now()
    s = stats.summary(db, "paper", now)
    assert s["trades"] == len(db.trades()) and 0 <= s["win_rate"] <= 1
    text = reports.daily_report(db, "paper", engine.risk.snapshot(), broker.account_info().to_dict(), now)
    assert "mode: PAPER" in text and "Audit log integrity: OK" in text and "Per strategy:" in text
    assert any(title.startswith("Daily report") for _, title, _ in session["cap"].sent)
    assert any(r["strategy"] for r in stats.by_strategy(db, "paper"))


def test_restart_never_duplicates_orders(bars_m5, tmp_path):
    path = tmp_path / "engine.db"
    db = Database(path)
    engine, feed, broker, _ = build(bars_m5, db)
    engine.start()
    run(engine, feed, 700)
    before = db.query("SELECT client_id, status FROM orders ORDER BY client_id")
    positions_before = sorted(p.ticket for p in broker.positions())
    assert before, "need at least one order before the restart"
    resume_at = feed._now
    db.close()

    # --- crash + restart: brand-new process state, same database -------------
    db2 = Database(path)
    engine2, feed2, broker2, _ = build(bars_m5, db2)
    feed2.set_time(resume_at)
    engine2.start()
    assert sorted(p.ticket for p in broker2.positions()) == positions_before       # positions restored
    engine2.run_once()                                                              # same bar again
    engine2.run_once()
    assert db2.query("SELECT client_id, status FROM orders ORDER BY client_id") == before
    # replaying bars the first process had already handled creates nothing new
    feed2.set_time(resume_at - pd.Timedelta(minutes=5))
    engine2._last_bar.clear()
    engine2._checked.clear()
    engine2.run_once()
    ids = [r["client_id"] for r in db2.query("SELECT client_id FROM orders")]
    assert len(ids) == len(set(ids))
    assert len(db2.query("SELECT 1 FROM orders")) >= len(before)
    tickets = [t["position_ticket"] for t in db2.trades()]
    assert len(tickets) == len(set(tickets))


def test_emergency_stop_flattens_and_blocks_until_resumed(bars_m5, db):
    engine, feed, broker, cap = build(bars_m5, db)
    engine.start()
    for _ in range(1500):
        feed.advance(1)
        engine.run_once()
        if broker.positions():
            break
    assert broker.positions(), "need an open position for this test"
    engine.risk.emergency_stop("operator pressed STOP", actor="dashboard")
    feed.advance(1)
    engine.run_once()
    assert broker.positions() == []
    assert any(level == "critical" and "EMERGENCY STOP" in title for level, title, _ in cap.sent)
    n_orders = len(db.query("SELECT 1 FROM orders"))
    totals = run(engine, feed, 400)
    assert totals["executed"] == 0 and len(db.query("SELECT 1 FROM orders WHERE status='filled'")) <= n_orders
    blocked = [d for d in db.decisions(500) if "emergency stop active" in d["explanation"]]
    assert blocked or totals["rejected"] == 0
    engine.risk.resume("operator", broker.account_info())
    assert engine.risk.can_open(broker.now())[0] is True


def test_max_drawdown_breach_suspends_and_flattens(bars_m5, db):
    engine, feed, broker, cap = build(bars_m5, db)
    engine.start()
    run(engine, feed, 3)
    broker._balance -= 600.0                                   # simulate a 6% equity collapse
    feed.advance(1)
    engine.run_once()
    assert engine.risk.state.suspended and broker.positions() == []
    assert any(level == "critical" and "max drawdown" in title for level, title, _ in cap.sent)
    assert run(engine, feed, 300)["executed"] == 0


def test_connection_loss_and_recovery(bars_m5, db):
    engine, feed, broker, cap = build(bars_m5, db)
    engine.start()
    run(engine, feed, 5)
    feed.connected = False
    out = engine.run_once()
    assert out["connected"] is False and db.heartbeats()["engine"]["status"] == "disconnected"
    assert any("Connection to broker lost" in title for _, title, _ in cap.sent)
    feed.connected = True
    engine._next_reconnect = 0.0
    assert engine.run_once()["connected"] is True
    assert any("Connection restored" in title for _, title, _ in cap.sent)
    events = [e["event"] for e in db.audit_entries(50)]
    assert "connection_lost" in events and "reconnected" in events


def test_news_block_prevents_entries(bars_m5, db):
    class AlwaysBlocked:
        def refresh(self, now, force=False):
            return True

        def state(self, currencies, now):
            return NewsState(blocked=True, reason="high-impact event window: USD CPI", risk_factor=0.0)

    engine, feed, broker, _ = build(bars_m5, db, news=AlwaysBlocked())
    engine.start()
    totals = run(engine, feed, 800)
    assert totals["executed"] == 0 and not broker.positions()
    assert totals["rejected"] > 0
    assert all("high-impact event window" in d["explanation"] for d in db.decisions(200))


def test_unvalidated_strategies_cannot_trade_when_validation_is_required(bars_m5, db):
    engine, feed, broker, _ = build(bars_m5, db, overrides={
        "strategies": {"enabled": list(M15), "min_confidence": 0.5, "allow_unvalidated_in_paper": False}})
    engine.start()
    totals = run(engine, feed, 700)
    assert totals["executed"] == 0 and totals["rejected"] > 0
    assert any("only VALIDATED strategies" in d["explanation"] for d in db.decisions(200))
    for name in M15:
        db.set_strategy_status(name, "EURUSD", "validated", {})
    assert run(engine, feed, 1200)["executed"] > 0


def test_degraded_strategy_is_suspended_automatically(bars_m5, db):
    from mt5quant.core.models import ClosedTrade, Side
    engine, feed, broker, cap = build(bars_m5, db, strategies=("liquidity_sweep",))
    engine.start()
    t0 = broker.now()
    for i in range(30):                                         # a clearly broken live record
        when = t0 - timedelta(hours=30 - i)
        db.record_trade(ClosedTrade(900 + i, "EURUSD", Side.BUY, 0.04, 1.1, 1.095, when, when, profit=-20.0,
                                    risk_amount=20.0, strategy="liquidity_sweep"), "paper")
    engine._check_strategy_health("liquidity_sweep")
    assert db.strategy_status("liquidity_sweep", "EURUSD") == "suspended"
    assert any("Strategy suspended" in title for _, title, _ in cap.sent)
    assert run(engine, feed, 600)["executed"] == 0


# ------------------------------------------------------------------ mode gates
def test_paper_mode_refuses_a_real_broker_and_demo_refuses_the_simulator(bars_m5, db):
    cfg = load_config(use_local=False, overrides={"symbols": ["EURUSD"]})
    mt5 = MT5Broker(TradingMode.PAPER, None, 1, mt5_module=FakeMT5())
    with pytest.raises(EngineError, match="PAPER mode requires the internal simulator"):
        TradingEngine(cfg, mt5, db).start()
    demo_cfg = load_config(use_local=False, overrides={"symbols": ["EURUSD"], "mode": "demo"})
    with pytest.raises(EngineError, match="requires a MetaTrader connection"):
        TradingEngine(demo_cfg, PaperBroker(), db).start()
    with pytest.raises(EngineError, match="no symbols"):
        TradingEngine(load_config(use_local=False, overrides={"symbols": []}), PaperBroker(), db).start()


def test_demo_engine_on_fake_terminal_and_live_stays_locked(db):
    demo_cfg = load_config(use_local=False, overrides={"symbols": ["EURUSD"], "mode": "demo",
                                                       "news": {"enabled": False}})
    fake = FakeMT5(demo=True)
    broker = MT5Broker(TradingMode.DEMO, LiveGate(demo_cfg, db, auth_file="/nonexistent"), 550100, mt5_module=fake)
    engine = TradingEngine(demo_cfg, broker, db, live_gate=LiveGate(demo_cfg, db, auth_file="/nonexistent"),
                           sleep=lambda s: None)
    engine.start()
    assert engine.run_once()["connected"] is True              # full cycle against the adapter
    assert db.heartbeats()["engine"]["details"]["broker"] == "mt5"

    with pytest.raises(EngineError, match="not available at the broker"):
        bad = load_config(use_local=False, overrides={"symbols": ["EURUSD.zz"], "mode": "demo"})
        TradingEngine(bad, MT5Broker(TradingMode.DEMO, LiveGate(bad, db, auth_file="/nonexistent"), 1,
                                     mt5_module=FakeMT5()), db).start()

    live_cfg = load_config(use_local=False, overrides={"symbols": ["EURUSD"], "mode": "live"})
    real = FakeMT5(demo=False)
    gate = LiveGate(live_cfg, db, auth_file="/nonexistent")
    with pytest.raises(LiveTradingLocked):
        TradingEngine(live_cfg, MT5Broker(TradingMode.LIVE, gate, 1, mt5_module=real), db, live_gate=gate).start()
    assert real.sent == []


def test_engine_survives_cycle_errors_and_trips_emergency_stop(bars_m5, db):
    engine, feed, broker, cap = build(bars_m5, db)
    engine.start()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 12:
            engine.running = False
        raise RuntimeError("unexpected bug")

    engine.run_once = boom
    engine.run_forever()
    assert engine.risk.kill_switch()["active"] is True
    assert any("Engine unstable" in title for _, title, _ in cap.sent)
    assert db.heartbeats()["engine"]["status"] == "stopped"
