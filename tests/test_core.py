import logging

import pytest
from pydantic import ValidationError

from mt5quant.core.config import AppConfig, load_config
from mt5quant.core.logging_setup import RedactionFilter, mask_login, redact
from mt5quant.core.models import ClosedTrade, Side, TradingMode, utcnow
from mt5quant.database.db import Database
from mt5quant.risk_management.limits import HARD_CEILINGS, RiskLimits


def test_default_config_is_paper_and_conservative(config):
    assert config.mode is TradingMode.PAPER
    assert config.risk.risk_per_trade_pct == 0.25
    assert config.risk.max_daily_loss_pct == 1.0
    assert config.risk.max_drawdown_pct == 5.0


def test_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"risk_per_trade_pct": 0.25, "no_such_limit": 1}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"typo_section": {}})


@pytest.mark.parametrize("name,ceiling", sorted(HARD_CEILINGS.items()))
def test_limits_cannot_exceed_hard_ceiling(name, ceiling):
    with pytest.raises(ValidationError):
        RiskLimits(**{name: type(RiskLimits.model_fields[name].default)(ceiling * 2 + 1)})


@pytest.mark.parametrize("bad", [
    {"risk_per_trade_pct": 0}, {"risk_per_trade_pct": None}, {"max_daily_loss_pct": -1},
    {"max_drawdown_pct": None}, {"max_open_positions": 0}, {"allow_martingale": True},
    {"allow_grid": True}, {"allow_averaging_down": True}, {"min_margin_level_pct": 50},
    {"max_daily_loss_pct": 0.1},      # below risk per trade
])
def test_limits_cannot_be_removed_or_disabled(bad):
    with pytest.raises(ValidationError):
        RiskLimits(**bad)


def test_limits_are_immutable():
    limits = RiskLimits()
    with pytest.raises(ValidationError):
        limits.risk_per_trade_pct = 1.0


def test_limits_can_be_tightened():
    assert RiskLimits(risk_per_trade_pct=0.1, max_daily_loss_pct=0.5).risk_per_trade_pct == 0.1


def test_load_config_override_cannot_remove_limit():
    with pytest.raises(ValidationError):
        load_config(use_local=False, overrides={"risk": {"max_daily_loss_pct": None}})


def test_redaction_masks_secrets(monkeypatch):
    monkeypatch.setenv("MT5_PASSWORD", "SuperSecret99")
    assert "SuperSecret99" not in redact("login with SuperSecret99 failed")
    assert "hunter2" not in redact("password=hunter2 server=x")
    assert "abc123xyz" not in redact('{"api_key": "abc123xyz"}')
    assert "123456:ABCDEFGHIJKLMNOPQRSTUVWX" not in redact(
        "https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWX/sendMessage")
    assert mask_login(12345678) == "12****78"


def test_redaction_filter_on_log_record(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_SECRET", "s3cr3t-value-0123456789")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "connecting with %s", ("s3cr3t-value-0123456789",), None)
    RedactionFilter().filter(rec)
    assert "s3cr3t-value" not in rec.getMessage()


def test_order_reservation_is_idempotent(db: Database):
    kw = dict(mode="paper", symbol="EURUSD", side="buy", order_type="market", volume=0.1, price=None,
              sl=1.0, tp=1.2, strategy="s", risk_amount=25.0, management={}, signal_time=utcnow())
    assert db.reserve_order(client_id="abc", **kw) is True
    assert db.reserve_order(client_id="abc", **kw) is False
    assert db.get_order("abc")["status"] == "submitting"
    db.update_order("abc", status="filled", position_ticket=7)
    assert db.order_for_position(7)["client_id"] == "abc"


def test_trade_recorded_once(db: Database):
    t = ClosedTrade(position_ticket=1, symbol="EURUSD", side=Side.BUY, volume=0.1, price_open=1.1,
                    price_close=1.101, time_open=utcnow(), time_close=utcnow(), profit=10.0, risk_amount=25.0)
    assert db.record_trade(t, "paper") is True
    assert db.record_trade(t, "paper") is False
    assert len(db.trades()) == 1
    assert db.trades()[0]["r_multiple"] == pytest.approx(0.4)


def test_audit_chain_detects_tampering(db: Database):
    for i in range(5):
        db.audit("event", {"i": i})
    assert db.verify_audit_chain() == (True, 0)
    db._conn.execute("UPDATE audit_log SET details='{\"i\": 99}' WHERE id=3")
    ok, bad = db.verify_audit_chain()
    assert not ok and bad == 3


def test_audit_chain_detects_deletion(db: Database):
    for i in range(4):
        db.audit("event", {"i": i})
    db._conn.execute("DELETE FROM audit_log WHERE id=2")
    assert db.verify_audit_chain()[0] is False


def test_state_survives_reopen(tmp_path):
    path = tmp_path / "x.db"
    d = Database(path)
    d.set_state("risk", {"peak": 10500.0})
    d.close()
    assert Database(path).get_state("risk") == {"peak": 10500.0}
