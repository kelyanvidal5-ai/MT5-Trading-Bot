"""Order lifecycle: idempotency, retries, uncertain results, reconciliation."""
from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.backtesting.costs import CostModel
from mt5quant.core.models import ManagementPlan, OrderResult, Side, Signal, TradingMode
from mt5quant.execution import broker as B
from mt5quant.execution.order_manager import OrderManager, make_client_id
from mt5quant.execution.paper import PaperBroker
from mt5quant.risk_management.manager import RiskDecision

UTC = timezone.utc
T0 = datetime(2024, 3, 13, 12, 0, tzinfo=UTC)


def paper() -> PaperBroker:
    b = PaperBroker(balance=10_000, cost=CostModel(slippage_points_mean=0, execution_delay_ms=0))
    b.set_price("EURUSD", 1.1000, 1.1001, T0)
    return b


def signal(minute=0, side=Side.BUY, strategy="trend_ema") -> Signal:
    sl, tp = (1.0950, 1.1100) if side is Side.BUY else (1.1050, 1.0900)
    return Signal(strategy=strategy, symbol="EURUSD", timeframe="M15", time=T0 + timedelta(minutes=minute),
                  side=side, sl=sl, tp=tp, ref_price=1.1, management=ManagementPlan(breakeven_at_r=1.0, atr=0.001))


DECISION = RiskDecision(approved=True, volume=0.04, risk_amount=20.4, risk_pct=0.2, entry_price=1.1001)


def manager(broker, db, **kw) -> OrderManager:
    return OrderManager(broker, db, TradingMode.PAPER, magic=550100, sleep=lambda s: None, **kw)


class Flaky(PaperBroker):
    """Paper broker whose send_order can be scripted."""

    def __init__(self, script):
        super().__init__(balance=10_000, cost=CostModel(slippage_points_mean=0, execution_delay_ms=0))
        self.set_price("EURUSD", 1.1000, 1.1001, T0)
        self.script = list(script)
        self.calls = 0

    def send_order(self, request):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "ok":
            return super().send_order(request)
        if step == "executed_but_timeout":          # broker filled, answer got lost
            super().send_order(request)
            return OrderResult(ok=False, retcode=B.RC_TIMEOUT, message="timeout", uncertain=True)
        if step == "timeout":
            return OrderResult(ok=False, retcode=B.RC_TIMEOUT, message="timeout", uncertain=True)
        if step == "raise":
            raise B.BrokerError("socket closed")
        return B.result_from_retcode(step)


def test_client_id_is_deterministic_and_fits_mt5_comment():
    a, b = make_client_id(signal()), make_client_id(signal())
    assert a == b and len(a) == 16 and len(a) <= 31
    assert make_client_id(signal(minute=15)) != a
    assert make_client_id(signal(side=Side.SELL)) != a
    assert make_client_id(signal(strategy="other")) != a


def test_submit_fills_and_records(db):
    b = paper()
    om = manager(b, db)
    status, result, cid = om.submit(signal(), DECISION)
    assert status == "filled" and result.ok
    row = db.get_order(cid)
    assert row["status"] == "filled" and row["position_ticket"] == result.position_ticket
    assert row["risk_amount"] == 20.4 and row["strategy"] == "trend_ema"
    pos = b.positions()[0]
    assert pos.comment == cid and pos.magic == 550100 and pos.sl == 1.0950 and pos.tp == 1.1100
    events = [e["event"] for e in db.audit_entries()]
    assert "order_submit" in events and "order_result" in events


def test_same_decision_is_never_sent_twice(db):
    b = paper()
    om = manager(b, db)
    assert om.submit(signal(), DECISION)[0] == "filled"
    assert om.submit(signal(), DECISION)[0] == "duplicate"
    # a brand-new OrderManager (i.e. after a restart) sees the same reservation
    assert manager(b, db).submit(signal(), DECISION)[0] == "duplicate"
    assert len(b.positions()) == 1


def test_retry_only_when_definitely_not_executed(db):
    b = Flaky([B.RC_REQUOTE, B.RC_PRICE_OFF, "ok"])
    status, _, _ = manager(b, db, max_retries=2).submit(signal(), DECISION)
    assert status == "filled" and b.calls == 3 and len(b.positions()) == 1
    b2 = Flaky([B.RC_REQUOTE] * 5)
    status, res, _ = manager(b2, db, max_retries=2).submit(signal(15), DECISION)
    assert status == "rejected" and b2.calls == 3
    b3 = Flaky([B.RC_INVALID_STOPS, "ok"])
    assert manager(b3, db).submit(signal(30), DECISION)[0] == "rejected" and b3.calls == 1


def test_timeout_after_execution_is_recovered_not_resent(db):
    b = Flaky(["executed_but_timeout"])
    status, _, cid = manager(b, db).submit(signal(), DECISION)
    assert status == "filled" and b.calls == 1 and len(b.positions()) == 1
    assert db.get_order(cid)["position_ticket"] == b.positions()[0].ticket


@pytest.mark.parametrize("failure", ["timeout", "raise"])
def test_timeout_without_execution_stays_unknown_then_fails_safely(db, failure):
    b = Flaky([failure])
    om = manager(b, db)
    status, _, cid = om.submit(signal(), DECISION)
    assert status == "unknown" and b.calls == 1 and not b.positions()
    assert om.reconcile(T0) == []                                            # inside the grace period
    changes = om.reconcile(T0, grace=timedelta(0))
    assert changes == [{"client_id": cid, "status": "failed"}]
    assert om.submit(signal(), DECISION)[0] == "duplicate" and b.calls == 1   # never resent


def test_crash_between_reservation_and_send(db):
    """Row left in 'submitting' by a crash: recovered from the broker state."""
    b = paper()
    om = manager(b, db)
    cid = make_client_id(signal())
    db.reserve_order(client_id=cid, mode="paper", symbol="EURUSD", side="buy", order_type="market", volume=0.04,
                     price=None, sl=1.095, tp=1.11, strategy="trend_ema", risk_amount=20.0, management={},
                     signal_time=T0, timeframe="M15")
    # case A: the order never reached the broker
    assert om.reconcile(T0) == []                                            # grace period first
    assert om.reconcile(T0, grace=timedelta(0)) == [{"client_id": cid, "status": "failed"}]
    # case B: it did reach the broker before the crash
    sig2 = signal(15)
    cid2 = make_client_id(sig2)
    db.reserve_order(client_id=cid2, mode="paper", symbol="EURUSD", side="buy", order_type="market", volume=0.04,
                     price=None, sl=1.095, tp=1.11, strategy="trend_ema", risk_amount=20.0, management={},
                     signal_time=sig2.time, timeframe="M15")
    from mt5quant.core.models import OrderRequest
    b.send_order(OrderRequest(cid2, "EURUSD", Side.BUY, 0.04, sl=1.095, tp=1.11, magic=550100, comment=cid2))
    assert om.reconcile(T0) == [{"client_id": cid2, "status": "filled"}]
    assert db.get_order(cid2)["position_ticket"] == b.positions()[0].ticket
    assert om.submit(sig2, DECISION)[0] == "duplicate" and len(b.positions()) == 1


def test_closed_trades_recorded_once_with_strategy_and_risk(db):
    b = paper()
    om = manager(b, db)
    _, res, cid = om.submit(signal(), DECISION)
    b.process_bar("EURUSD", T0, 1.1, 1.1105, 1.0999, 1.11, 0.0001, T0 + timedelta(minutes=15))
    fresh = om.sync_closed_trades(T0 - timedelta(hours=1))
    assert len(fresh) == 1
    t = fresh[0]
    assert t.strategy == "trend_ema" and t.client_id == cid and t.exit_reason == "tp"
    assert t.risk_amount > 0 and t.r_multiple > 1.5
    assert om.sync_closed_trades(T0 - timedelta(hours=1)) == []              # idempotent
    assert len(db.trades(mode="paper")) == 1


def test_foreign_trades_are_ignored(db):
    b = paper()
    om = manager(b, db)
    from mt5quant.core.models import OrderRequest
    b.send_order(OrderRequest("manual", "EURUSD", Side.BUY, 0.1, sl=1.09, tp=1.12, magic=999))
    b.close_position(b.positions()[0].ticket)
    assert om.sync_closed_trades(T0 - timedelta(hours=1)) == [] and db.trades() == []
    b.send_order(OrderRequest("manual2", "EURUSD", Side.BUY, 0.1, sl=1.09, tp=1.12, magic=999))
    assert om.flatten("emergency")["closed"] == 0 and len(b.positions()) == 1   # never touches foreign positions


def test_flatten_closes_own_positions_and_orders(db):
    b = paper()
    om = manager(b, db)
    om.submit(signal(), DECISION)
    om.submit(signal(15, strategy="x"), DECISION)
    res = om.flatten("emergency_stop")
    assert res == {"closed": 2, "failed": 0, "cancelled": 0} and not b.positions()
    assert all(t.exit_reason == "emergency_stop" for t in b.all_closed_trades)


def test_modify_stop_and_management_lookup(db):
    b = paper()
    om = manager(b, db)
    _, res, cid = om.submit(signal(), DECISION)
    pos = b.positions()[0]
    info = om.management_for(pos)
    assert info["sl_initial"] == 1.0950 and info["management"]["breakeven_at_r"] == 1.0
    assert info["timeframe"] == "M15" and info["client_id"] == cid
    assert om.modify_stop(pos, 1.0980, "trailing") and b.positions()[0].sl == 1.0980
    assert any(e["event"] == "stop_moved" for e in db.audit_entries())
