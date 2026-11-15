import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.fake_mt5 import FakeMT5
from mt5quant.bridge.codec import auth_headers, decode, encode, sign
from mt5quant.bridge.server import create_gateway_app
from mt5quant.core.config import load_config
from mt5quant.core.models import OrderRequest, OrderType, Side, TradingMode, utcnow
from mt5quant.execution import broker as B
from mt5quant.execution.bridge_client import BridgeBroker
from mt5quant.execution.live_gate import LiveGate
from mt5quant.execution.mt5_broker import MT5Broker
from mt5quant.execution.paper import PaperBroker
from mt5quant.market_data.feeds import ReplayFeed

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef"
T0 = datetime(2024, 3, 13, 12, 0, tzinfo=timezone.utc)


def wire(broker):
    client = TestClient(create_gateway_app(broker, SECRET))

    def transport(url, body, headers, timeout):
        r = client.post("/rpc", content=body, headers=headers)
        return r.status_code, r.json()

    return client, BridgeBroker("http://gateway", SECRET, transport=transport)


def paper():
    b = PaperBroker(balance=10_000)
    b.set_price("EURUSD", 1.1000, 1.1001, T0)
    return b


def test_codec_roundtrip(bars_m5):
    r = OrderRequest("cid", "EURUSD", Side.SELL, 0.1, sl=1.2, tp=1.0, order_type=OrderType.LIMIT, price=1.15,
                     expiry=T0)
    back = decode(json.loads(json.dumps(encode({"request": r}))))["request"]
    assert back == r
    df = decode(json.loads(json.dumps(encode(bars_m5.iloc[:50]))))
    assert (df.index == bars_m5.index[:50]).all() and df["close"].iloc[-1] == bars_m5["close"].iloc[49]


def test_gateway_requires_strong_secret():
    with pytest.raises(ValueError):
        create_gateway_app(paper(), "short")


def test_unauthenticated_and_tampered_requests_rejected():
    client, _ = wire(paper())
    body = json.dumps({"method": "account_info", "params": {}}).encode()
    assert client.post("/rpc", content=body).status_code == 401
    bad = auth_headers("wrong-secret-wrong-secret-wrong-secret", body)
    assert client.post("/rpc", content=body, headers=bad).status_code == 401
    good = auth_headers(SECRET, body)
    tampered = json.dumps({"method": "positions", "params": {}}).encode()
    assert client.post("/rpc", content=tampered, headers=good).status_code == 401
    assert client.post("/rpc", content=body, headers=good).status_code == 200


def test_replay_and_stale_timestamp_rejected():
    client, _ = wire(paper())
    body = json.dumps({"method": "account_info", "params": {}}).encode()
    h = auth_headers(SECRET, body)
    assert client.post("/rpc", content=body, headers=h).status_code == 200
    assert client.post("/rpc", content=body, headers=h).status_code == 401          # same nonce
    old = str(int(time.time()) - 3600)
    stale = {"X-Timestamp": old, "X-Nonce": "n1", "X-Signature": sign(SECRET, old, "n1", body)}
    assert client.post("/rpc", content=body, headers=stale).status_code == 401


def test_only_whitelisted_methods():
    client, _ = wire(paper())
    for method in ("load_state", "__init__", "_close", "disconnect", "to_state"):
        body = json.dumps({"method": method, "params": {}}).encode()
        assert client.post("/rpc", content=body, headers=auth_headers(SECRET, body)).status_code == 400
    assert client.get("/health").json() == {"status": "ok"}


def test_full_trading_roundtrip_through_bridge():
    backend = paper()
    _, remote = wire(backend)
    assert remote.connect()
    assert remote.account_info().balance == 10_000
    assert remote.symbol_spec("EURUSD").digits == 5
    assert remote.tick("EURUSD").ask == 1.1001
    r = remote.send_order(OrderRequest("cid-1", "EURUSD", Side.BUY, 0.1, sl=1.0950, tp=1.1100))
    assert r.ok and r.position_ticket
    pos = remote.positions()
    assert len(pos) == 1 and pos[0].side is Side.BUY and isinstance(pos[0].time_open, datetime)
    assert remote.modify_position(r.position_ticket, 1.0960, 1.1100).ok
    assert remote.close_position(r.position_ticket, reason="test").ok
    trades = remote.closed_trades(T0 - timedelta(days=1))
    assert len(trades) == 1 and trades[0].exit_reason == "test"
    bad = remote.send_order(OrderRequest("cid-2", "EURUSD", Side.BUY, 0.1, sl=1.2, tp=1.3))
    assert not bad.ok and bad.retcode == B.RC_INVALID_STOPS


def test_gateway_is_idempotent_on_client_id():
    backend = paper()
    _, remote = wire(backend)
    req = OrderRequest("same-id", "EURUSD", Side.BUY, 0.1, sl=1.0950, tp=1.1100)
    first, second = remote.send_order(req), remote.send_order(req)
    assert first.ok and second.position_ticket == first.position_ticket
    assert len(backend.positions()) == 1


def test_network_failure_is_reported_as_uncertain():
    def boom(url, body, headers, timeout):
        raise TimeoutError("timed out")

    remote = BridgeBroker("http://gateway", SECRET, transport=boom)
    r = remote.send_order(OrderRequest("x", "EURUSD", Side.BUY, 0.1, sl=1.0, tp=1.2))
    assert not r.ok and r.uncertain and not r.retryable
    assert remote.is_connected() is False
    with pytest.raises(B.BrokerError):
        remote.account_info()


def test_bridge_in_front_of_mt5_adapter_enforces_demo_gate():
    fake = FakeMT5(demo=True)
    cfg = load_config(use_local=False, overrides={"mode": "demo"})
    mt5 = MT5Broker(TradingMode.DEMO, LiveGate(cfg, auth_file="/nonexistent"), 550100, mt5_module=fake)
    mt5.connect()
    _, remote = wire(mt5)
    assert remote.send_order(OrderRequest("a1", "EURUSD", Side.BUY, 0.1, sl=1.09, tp=1.12)).ok
    fake.demo = False
    r = remote.send_order(OrderRequest("a2", "EURUSD", Side.BUY, 0.1, sl=1.09, tp=1.12))
    assert r.retcode == B.RC_LIVE_LOCKED and len(fake.positions) == 1
    bars = remote.rates("EURUSD", "H1", 20)
    assert len(bars) == 20 and bars.index.tz is not None


def test_replay_feed_hides_the_future(bars_m5):
    feed = ReplayFeed({"EURUSD": bars_m5}, base_tf="M5", start=bars_m5.index[20_000])
    now = feed.now()
    m5 = feed.rates("EURUSD", "M5", 100)
    assert m5.index[-1] + (m5.index[1] - m5.index[0]) <= now          # only completed bars
    h1 = feed.rates("EURUSD", "H1", 10)
    last_h1 = h1.index[-1]
    full_chunk = bars_m5[(bars_m5.index >= last_h1) & (bars_m5.index < now)]
    assert h1["high"].iloc[-1] == full_chunk["high"].max()            # nothing beyond `now`
    t = feed.tick("EURUSD")
    assert t.bid == bars_m5["open"].iloc[20_000]                      # forming bar exposes only its open
    feed.advance(1)
    assert feed.tick("EURUSD").bid == bars_m5["open"].iloc[20_001]
    feed.connected = False
    with pytest.raises(B.BrokerError):
        feed.rates("EURUSD", "M5", 5)
    assert utcnow() > now
