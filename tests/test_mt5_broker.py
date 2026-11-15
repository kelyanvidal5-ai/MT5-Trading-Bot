"""MT5 adapter logic against the fake terminal (see tests/fake_mt5.py).

These tests verify request construction, gating and result handling. They do
not prove behaviour against a real terminal - that requires a demo account.
"""
import json
import sys
from datetime import timedelta

import pandas as pd
import pytest

from tests.fake_mt5 import FakeMT5
from mt5quant.core.config import LIVE_AUTH_PHRASE, load_config
from mt5quant.core.models import ClosedTrade, OrderRequest, OrderType, Side, TradingMode, utcnow
from mt5quant.execution import broker as B
from mt5quant.execution.live_gate import LiveGate, LiveTradingLocked, write_authorization
from mt5quant.execution.mt5_broker import MT5Broker, load_mt5_module


def make(fake=None, mode=TradingMode.DEMO, config=None, db=None, auth=None, **kw):
    fake = fake or FakeMT5()
    config = config or load_config(use_local=False, overrides={"mode": mode.value})
    gate = LiveGate(config, db=db, auth_file=auth or "/nonexistent/auth.json")
    broker = MT5Broker(mode=mode, gate=gate, magic=550100, mt5_module=fake, **kw)
    return broker, fake


def req(**kw) -> OrderRequest:
    base = dict(client_id="q1a2b3c4d5e6f708", symbol="EURUSD", side=Side.BUY, volume=0.10, sl=1.0950, tp=1.1100,
                deviation_points=15, strategy="trend_ema")
    base.update(kw)
    return OrderRequest(**base)


def test_official_package_is_windows_only_and_error_is_explicit():
    if sys.platform == "win32":
        pytest.skip("only meaningful off Windows")
    with pytest.raises(B.BrokerError, match="only exists for Windows"):
        load_mt5_module()


def test_connect_reads_demo_account_and_masks_nothing_sensitive(monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "12345678")
    monkeypatch.setenv("MT5_PASSWORD", "pw-from-env")
    monkeypatch.setenv("MT5_SERVER", "Fake-Demo")
    broker, fake = make()
    assert broker.connect() is True
    assert fake.init_kwargs == {"login": 12345678, "password": "pw-from-env", "server": "Fake-Demo"}
    acct = broker.account_info()
    assert acct.is_demo and acct.balance == 10_000 and acct.currency == "USD"


def test_demo_mode_refuses_real_account():
    broker, fake = make(FakeMT5(demo=False))
    with pytest.raises(LiveTradingLocked, match="NOT a demo account"):
        broker.connect()
    assert fake.initialized is False


def test_live_mode_locked_without_full_authorization(db):
    broker, fake = make(FakeMT5(demo=False), mode=TradingMode.LIVE, db=db)
    with pytest.raises(LiveTradingLocked) as exc:
        broker.connect()
    msg = str(exc.value)
    assert "environment authorization phrase" in msg and "no live authorization file" in msg
    assert "demo track record" in msg


def test_live_gate_requires_every_key(db, tmp_path, monkeypatch):
    cfg = load_config(use_local=False, overrides={"mode": "live"})
    auth = tmp_path / "auth.json"
    gate = LiveGate(cfg, db=db, auth_file=auth)
    fake = FakeMT5(demo=False)
    broker = MT5Broker(mode=TradingMode.LIVE, gate=gate, magic=1, mt5_module=fake)
    fake.initialize()
    broker._initialized = True
    acct = broker.account_info()

    def blocked():
        return gate.check(acct)[1]

    assert len(blocked()) == 4
    monkeypatch.setenv("MT5QUANT_LIVE_AUTHORIZED", LIVE_AUTH_PHRASE)
    assert len(blocked()) == 3
    write_authorization("99999999", "tester", auth)               # wrong account
    assert any("different account" in r for r in blocked())
    write_authorization(acct.login, "tester", auth)
    assert all("track record" in r for r in blocked()) and len(blocked()) == 2
    # demo evidence: 60 trades over 40 days
    start = utcnow() - timedelta(days=45)
    for i in range(60):
        t = start + timedelta(days=i * 40 / 59)
        db.record_trade(ClosedTrade(i + 1, "EURUSD", Side.BUY, 0.1, 1.1, 1.101, t, t, 5.0, risk_amount=25), "demo")
    assert gate.check(acct) == (True, [])
    # expired authorization
    data = json.loads(auth.read_text())
    data["authorized_at"] = (utcnow() - timedelta(days=200)).isoformat()
    auth.write_text(json.dumps(data))
    assert any("expired" in r for r in blocked())
    # config flipped back to paper -> locked regardless of the other keys
    write_authorization(acct.login, "tester", auth)
    gate2 = LiveGate(load_config(use_local=False), db=db, auth_file=auth)
    assert gate2.check(acct)[0] is False


def test_market_order_request_shape_and_verification():
    broker, fake = make()
    broker.connect()
    r = broker.send_order(req())
    assert r.ok and r.retcode == B.RC_DONE and r.position_ticket == 500 and r.fill_price == 1.10010
    sent = fake.sent[-1]
    assert sent["action"] == fake.TRADE_ACTION_DEAL and sent["type"] == fake.ORDER_TYPE_BUY
    assert sent["price"] == 1.10010 and sent["sl"] == 1.0950 and sent["tp"] == 1.1100
    assert sent["deviation"] == 15 and sent["magic"] == 550100
    assert sent["comment"] == "q1a2b3c4d5e6f708" and len(sent["comment"]) <= 31
    assert sent["type_filling"] == fake.ORDER_FILLING_IOC          # symbol advertises IOC (flag 2)
    pos = broker.positions("EURUSD")
    assert len(pos) == 1 and pos[0].side is Side.BUY and pos[0].comment == "q1a2b3c4d5e6f708"


def test_pending_order_modify_close_cancel():
    broker, fake = make()
    broker.connect()
    r = broker.send_order(req(order_type=OrderType.LIMIT, price=1.0980, expiry=utcnow() + timedelta(hours=1)))
    assert r.ok and r.retcode == B.RC_PLACED
    assert fake.sent[-1]["type"] == fake.ORDER_TYPE_BUY_LIMIT
    assert fake.sent[-1]["type_time"] == fake.ORDER_TIME_SPECIFIED
    pend = broker.pending_orders()
    assert len(pend) == 1 and pend[0].order_type is OrderType.LIMIT and pend[0].expiry is not None
    assert broker.cancel_order(pend[0].ticket).ok and not broker.pending_orders()

    r = broker.send_order(req())
    assert broker.modify_position(r.position_ticket, 1.0970, 1.1100).ok
    assert fake.positions[r.position_ticket].sl == 1.0970
    fake.prices["EURUSD"] = (1.1030, 1.1031)
    c = broker.close_position(r.position_ticket, reason="time_exit")
    assert c.ok and fake.sent[-1]["type"] == fake.ORDER_TYPE_SELL and fake.sent[-1]["price"] == 1.1030
    assert broker.close_position(r.position_ticket).retcode == B.RC_POSITION_CLOSED
    trades = broker.closed_trades(utcnow() - timedelta(hours=1))
    assert len(trades) == 1
    t = trades[0]
    assert t.side is Side.BUY and t.client_id == "q1a2b3c4d5e6f708"
    assert t.profit == pytest.approx((1.1030 - 1.10010) * 0.1 * 100_000 - 0.70)


def test_rejections_and_uncertain_results():
    broker, fake = make()
    broker.connect()
    fake.next_retcode = B.RC_REQUOTE
    r = broker.send_order(req())
    assert not r.ok and r.retryable and not r.uncertain
    fake.next_retcode = B.RC_INVALID_STOPS
    r = broker.send_order(req())
    assert not r.ok and not r.retryable
    fake.next_retcode = B.RC_TIMEOUT
    assert broker.send_order(req()).uncertain
    fake.return_none = True
    r = broker.send_order(req())
    assert not r.ok and r.uncertain and not r.retryable


def test_read_only_adapter_never_sends_orders():
    broker, fake = make(mode=TradingMode.PAPER)
    assert broker.read_only
    broker.connect()                                # no gate check needed for read-only
    r = broker.send_order(req())
    assert r.retcode == B.RC_LIVE_LOCKED and fake.sent == []
    assert broker.close_position(1).retcode == B.RC_LIVE_LOCKED
    assert broker.tick("EURUSD").bid == 1.10000     # data still works


def test_per_order_guard_blocks_if_account_turns_real():
    broker, fake = make()
    broker.connect()
    fake.demo = False                               # e.g. operator switched account in the terminal
    r = broker.send_order(req())
    assert r.retcode == B.RC_LIVE_LOCKED and fake.sent == []


def test_server_time_offset_is_removed_from_bars_and_ticks():
    broker, fake = make(FakeMT5(server_offset_hours=3.0))
    broker.connect()
    broker.symbol_spec("EURUSD")
    tick = broker.tick("EURUSD")
    assert abs((tick.time - utcnow()).total_seconds()) < 5
    bars = broker.rates("EURUSD", "M5", 50)
    assert len(bars) == 50 and list(bars.columns) == ["open", "high", "low", "close", "volume", "spread"]
    assert bars.index.tz is not None
    assert abs((bars.index[-1] - pd.Timestamp(utcnow())).total_seconds()) <= 300


def test_symbol_spec_and_unknown_symbol():
    broker, _ = make()
    broker.connect()
    spec = broker.symbol_spec("EURUSD")
    assert spec.digits == 5 and spec.stops_level == 10 and spec.volume_step == 0.01
    assert spec.asset_class == "forex" and spec.value_per_price_unit() == pytest.approx(100_000)
    with pytest.raises(B.BrokerError, match="does not exist on this broker"):
        broker.symbol_spec("EURUSD.xyz")


def test_connection_state():
    broker, fake = make()
    assert broker.is_connected() is False
    fake.init_ok = False
    assert broker.connect() is False
    fake.init_ok = True
    assert broker.connect() and broker.is_connected()
    fake.connected = False
    assert broker.is_connected() is False
    with pytest.raises(B.BrokerError):
        MT5Broker(TradingMode.DEMO, None, 1, mt5_module=FakeMT5()).account_info()
