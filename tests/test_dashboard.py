from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mt5quant.core.config import load_config
from mt5quant.core.models import ClosedTrade, Side, utcnow
from mt5quant.dashboard.app import create_app
from mt5quant.risk_management.manager import RiskManager


def seed(db):
    now = utcnow()
    for i, profit in enumerate([40.0, -25.0, 60.0, -25.0]):
        t = now - timedelta(hours=10 - i)
        db.record_trade(ClosedTrade(i + 1, "EURUSD", Side.BUY, 0.04, 1.1, 1.101, t - timedelta(hours=1), t,
                                    profit=profit, risk_amount=25.0, strategy="trend_ema", exit_reason="tp"),
                        "paper")
    for i in range(5):
        db.snapshot_equity(time=now - timedelta(minutes=5 - i), mode="paper", balance=10_050.0,
                           equity=10_040.0 + i, margin=0, margin_free=10_040.0, open_positions=1, open_risk=20.0)
    db.set_state("positions", [{"ticket": 7, "symbol": "EURUSD", "side": "buy", "volume": 0.04,
                                "price_open": 1.1, "price_current": 1.1012, "sl": 1.095, "tp": 1.11,
                                "profit": 4.8, "swap": 0.0, "time_open": now.isoformat(), "strategy": "trend_ema",
                                "risk": 20.0, "digits": 5}])
    db.heartbeat("engine", "running", {
        "mode": "paper", "engine_time": now.isoformat(), "cycle_ms": 12.0, "can_open": True, "blocks": [],
        "markets": {"EURUSD": "monitoring"}, "sessions": ["london"], "broker": "paper", "synthetic_data": True,
        "account": {"login": "PA**R", "server": "sim", "currency": "USD", "balance": 10_050.0, "equity": 10_044.8,
                    "margin": 146.0, "margin_free": 9_898.8, "margin_level": 6880.0, "leverage": 30,
                    "is_demo": True, "trade_allowed": True, "profit": 4.8}})
    db.record_decision(time=now, symbol="EURUSD", strategy="trend_ema", side="buy", action="executed",
                       confidence=0.71, regime="trend_up", explanation="EXECUTED BUY EURUSD", details={})
    db.log_notification("info", "Trade opened", "body", "log")


@pytest.fixture()
def client(db, config):
    seed(db)
    return TestClient(create_app(config, db))


def test_state_contains_every_dashboard_metric(client):
    s = client.get("/api/state").json()
    assert s["mode"] == "paper" and s["health"]["alive"] and s["connection"]["connected"]
    assert s["connection"]["synthetic_data"] is True
    a = s["account"]
    assert a["balance"] == 10_050.0 and a["equity"] == 10_044.8 and a["margin_free"] == 9_898.8 and a["profit"] == 4.8
    sm = s["summary"]
    assert sm["trades"] == 4 and sm["win_rate"] == 0.5 and sm["net_profit"] == 50.0
    assert sm["avg_win"] == 50.0 and sm["avg_loss"] == -25.0 and sm["profit_factor"] == 2.0
    assert sm["pnl_today"] is not None and sm["pnl_week"] is not None and sm["pnl_month"] is not None
    assert "max_drawdown_pct" in sm and sm["expectancy_r"] == pytest.approx(0.5)
    assert s["risk"]["limits"]["risk_per_trade_pct"] == 0.25 and s["risk"]["kill_switch"]["active"] is False
    assert s["exposure"]["open_risk"] == 20.0 and s["exposure"]["positions"] == 1
    assert s["positions"][0]["strategy"] == "trend_ema"
    assert s["markets"] == {"EURUSD": "monitoring"} and s["audit_ok"] is True
    names = [x["name"] for x in s["strategies"]]
    assert len(names) == 9 and "trend_ema" in names
    row = next(x for x in s["strategies"] if x["name"] == "trend_ema")
    assert row["trades"] == 4 and row["status"] == "untested"


def test_equity_trades_decisions_endpoints(client):
    eq = client.get("/api/equity?range=7d").json()
    assert len(eq["points"]) == 5 and eq["points"][-1]["equity"] == 10_044.0 and eq["daily"]
    assert client.get("/api/equity?range=bogus").status_code == 400
    trades = client.get("/api/trades?limit=2").json()
    assert len(trades) == 2 and {"profit", "r_multiple", "strategy", "exit_reason"} <= set(trades[0])
    assert client.get("/api/decisions").json()[0]["action"] == "executed"
    page = client.get("/")
    assert page.status_code == 200 and "EMERGENCY STOP" in page.text
    assert client.get("/static/app.js").status_code == 200


def test_security_headers_and_no_inline_script(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert "<script>" not in r.text and "onclick=" not in r.text and 'style="' not in r.text
    assert client.get("/api/state").headers["cache-control"] == "no-store"
    js = client.get("/static/app.js").text
    assert "innerHTML" not in js.replace("never innerHTML", "")


def test_emergency_stop_from_loopback_without_token(client, db, config):
    r = client.post("/api/emergency-stop", json={"reason": "test stop"})
    assert r.status_code == 200
    ks = RiskManager(config.risk, db).kill_switch()
    assert ks["active"] and ks["reason"] == "test stop" and ks["actor"] == "dashboard"
    assert any(e["event"] == "emergency_stop" and e["actor"] == "dashboard" for e in db.audit_entries())
    assert client.get("/api/state").json()["risk"]["kill_switch"]["active"] is True
    # resume is impossible without a configured token
    assert client.post("/api/resume", json={"confirm": True}).status_code == 403
    assert RiskManager(config.risk, db).kill_switch()["active"] is True


def test_token_protects_everything_when_configured(db, config, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok-0123456789abcdef")
    seed(db)
    c = TestClient(create_app(config, db))
    assert c.get("/api/meta").json() == {"token_required": True, "mode": "paper"}
    for path in ("/api/state", "/api/equity", "/api/trades", "/api/decisions"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-Dashboard-Token": "wrong"}).status_code == 401
    h = {"X-Dashboard-Token": "tok-0123456789abcdef"}
    assert c.get("/api/state", headers=h).status_code == 200
    assert c.post("/api/emergency-stop", json={}).status_code == 401
    assert c.post("/api/emergency-stop", json={}, headers=h).status_code == 200
    assert c.post("/api/resume", json={}, headers=h).status_code == 400            # needs explicit confirm
    assert c.post("/api/resume", json={"confirm": True}).status_code == 401
    assert c.post("/api/resume", json={"confirm": True}, headers=h).status_code == 200
    assert RiskManager(config.risk, db).kill_switch()["active"] is False


def test_refuses_public_bind_without_token(db):
    cfg = load_config(use_local=False, overrides={"dashboard": {"host": "0.0.0.0"}})
    with pytest.raises(RuntimeError, match="without DASHBOARD_TOKEN"):
        create_app(cfg, db)


def test_dashboard_reports_dead_engine(db, config):
    c = TestClient(create_app(config, db))
    s = c.get("/api/state").json()
    assert s["health"]["alive"] is False and s["connection"]["connected"] is False and s["account"] is None
    assert s["summary"]["trades"] == 0 and s["summary"]["profit_factor"] is None
