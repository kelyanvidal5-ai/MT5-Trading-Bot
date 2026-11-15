"""Risk-management tests: sizing, pre-trade checks, circuit breakers."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.core.models import (AccountInfo, ClosedTrade, ManagementPlan, Position, Side, Signal, Tick)
from mt5quant.execution.position_manager import plan
from mt5quant.market_data.specs import generic_spec
from mt5quant.risk_management.limits import RiskLimits
from mt5quant.risk_management.manager import RiskManager, remaining_risk
from mt5quant.risk_management.sizing import floor_to_step, loss_per_lot, position_size

UTC = timezone.utc
NOW = datetime(2024, 3, 13, 12, 0, tzinfo=UTC)          # a Wednesday
EUR = generic_spec("EURUSD")
SPECS = {s: generic_spec(s) for s in ("EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "EURGBP")}


def account(equity=10_000.0, margin=0.0, **kw) -> AccountInfo:
    base = dict(login="1", server="s", currency="USD", balance=equity, equity=equity, margin=margin,
                margin_free=equity - margin, margin_level=(equity / margin * 100 if margin else 0.0),
                leverage=30, is_demo=True, trade_allowed=True)
    base.update(kw)
    return AccountInfo(**base)


def signal(symbol="EURUSD", side=Side.BUY, ref=1.1000, sl=1.0950, tp=1.1100, **kw) -> Signal:
    return Signal(strategy="test", symbol=symbol, timeframe="M15", time=NOW, side=side, sl=sl, tp=tp,
                  ref_price=ref, **kw)


def tick(bid=1.1000, spread=0.00008, symbol="EURUSD") -> Tick:
    return Tick(symbol, NOW, bid, round(bid + spread, 5))


def position(symbol="EURUSD", side=Side.BUY, volume=0.05, open_=1.1000, sl=1.0950, ticket=1) -> Position:
    return Position(ticket=ticket, symbol=symbol, side=side, volume=volume, price_open=open_, sl=sl, tp=1.2,
                    time_open=NOW, price_current=open_)


@pytest.fixture()
def rm(db) -> RiskManager:
    m = RiskManager(RiskLimits(), db)
    m.update(account(), NOW)
    return m


def check(rm, sig=None, acct=None, positions=(), tk=None, atr=0.0020, trades_today=0, **kw):
    sig = sig or signal()
    return rm.evaluate(sig, acct or account(), list(positions), tk or tick(), SPECS[sig.symbol], SPECS, NOW,
                       atr, trades_today, **kw)


# --------------------------------------------------------------------- sizing
def test_position_size_risks_exactly_the_budget_rounded_down():
    # 0.25% of 10k = 25 USD ; stop 50.8 pips from the ask ; 1 lot = 10 USD/pip
    vol, risk = position_size(10_000, 0.25, 1.10008, 1.0950, EUR)
    assert vol == 0.04 and risk == pytest.approx(0.04 * 508.0)
    assert risk <= 25.0
    assert loss_per_lot(1.1, 1.095, EUR) == pytest.approx(500.0)


def test_position_size_never_rounds_up_to_min_lot():
    assert position_size(1_000, 0.25, 1.1000, 1.0900, EUR) == (0.0, 0.0)     # would need 0.0025 lots
    assert position_size(10_000, 0.25, 1.1, 1.1, EUR) == (0.0, 0.0)          # zero stop distance
    assert position_size(0, 0.25, 1.1, 1.09, EUR) == (0.0, 0.0)
    assert floor_to_step(0.0379, EUR) == 0.03 and floor_to_step(0.3, EUR) == 0.3


def test_size_shrinks_after_losses_never_grows(rm):
    """No martingale: a losing streak can only reduce the next position."""
    sizes, equity = [], 1_000_000.0          # large account so the 0.01-lot step does not mask the effect
    for _ in range(12):
        d = check(rm, acct=account(equity))
        sizes.append(d.risk_amount)
        assert d.risk_pct <= 0.25 + 1e-9
        equity -= d.risk_amount                                   # the trade loses
    assert all(b <= a + 1e-9 for a, b in zip(sizes, sizes[1:]))
    assert sizes[-1] < sizes[0]


# ----------------------------------------------------------- pre-trade checks
def test_approves_clean_trade(rm):
    d = check(rm)
    assert d.approved and d.volume == 0.04 and not d.reasons
    assert 0 < d.risk_pct <= 0.25 and d.entry_price == 1.10008


@pytest.mark.parametrize("sig,expected", [
    (signal(sl=0.0), "no stop-loss"),
    (signal(tp=0.0), "no take-profit"),
    (signal(sl=1.1050), "wrong side"),
    (signal(tp=1.0990), "wrong side"),
    (signal(sl=1.1000), "too close"),
    (signal(tp=1.1020), "reward/risk"),
    (signal(ref=1.0940, sl=1.0900, tp=1.1300), "price moved away"),
])
def test_rejects_invalid_signals(rm, sig, expected):
    d = check(rm, sig)
    assert not d.approved and d.volume == 0.0
    assert any(expected in r for r in d.reasons), d.reasons


def test_spread_protection(rm):
    d = check(rm, tk=tick(spread=0.0006), atr=0.0020)             # spread 30% of ATR
    assert not d.approved and any("spread" in r for r in d.reasons)
    assert check(rm, tk=tick(spread=0.0002), atr=0.0020).approved  # 10% of ATR


def test_position_limits_and_no_hedging_or_averaging(rm):
    three = [position("GBPUSD", ticket=1, volume=0.01), position("AUDUSD", ticket=2, volume=0.01),
             position("USDCHF", ticket=3, volume=0.01, side=Side.SELL, sl=1.0)]
    d = check(rm, positions=three)
    assert any("maximum open positions" in r for r in d.reasons)
    same = check(rm, positions=[position("EURUSD")])
    assert any("already 1 position" in r for r in same.reasons)
    assert any("averaging" in r for r in same.reasons)            # existing long still at risk
    hedge = check(rm, signal(side=Side.SELL, sl=1.1050, tp=1.0900), positions=[position("EURUSD")])
    assert any("no hedging" in r for r in hedge.reasons)
    assert any("maximum trades per day" in r for r in check(rm, trades_today=10).reasons)


def test_pyramiding_only_onto_risk_free_position(db):
    rm = RiskManager(RiskLimits(max_positions_per_symbol=2), db)
    rm.update(account(), NOW)
    at_risk = check(rm, positions=[position("EURUSD", volume=0.02)])
    assert any("averaging" in r for r in at_risk.reasons)
    risk_free = check(rm, positions=[position("EURUSD", volume=0.02, open_=1.0900, sl=1.0905)])
    assert risk_free.approved


def test_correlated_exposure_limit(rm):
    """Long EURUSD + long GBPUSD + long AUDUSD are all short USD."""
    usd_short = [position("GBPUSD", ticket=1, volume=0.04, open_=1.27, sl=1.2650),      # 20 USD risk
                 position("AUDUSD", ticket=2, volume=0.05, open_=0.66, sl=0.6560)]      # 20 USD risk
    d = check(rm, positions=usd_short)                             # +20 more = 60 USD = 0.6% net USD
    assert not d.approved and any("correlated exposure" in r and "USD" in r for r in d.reasons)
    # A USD-long trade offsets instead of stacking -> allowed.
    offset = check(rm, signal("USDCHF", ref=0.8800, sl=0.8760, tp=0.8900), positions=usd_short[:1],
                   tk=Tick("USDCHF", NOW, 0.8800, 0.88008))
    assert offset.approved, offset.reasons


def test_total_open_risk_and_daily_headroom(rm):
    big = [position("GBPUSD", ticket=1, volume=0.12, open_=1.27, sl=1.2650)]            # 60 USD = 0.6%
    d = check(rm, signal("EURGBP", ref=0.8550, sl=0.8520, tp=0.8650), positions=big,
              tk=Tick("EURGBP", NOW, 0.8550, 0.85508))
    assert any("total open risk" in r for r in d.reasons)
    # realised loss of 0.8% today leaves no room for another 0.25% risk
    rm.update(account(9_920), NOW)
    d2 = check(rm, acct=account(9_920))
    assert any("daily loss limit" in r for r in d2.reasons)


def test_refuses_to_add_risk_when_a_position_has_no_stop(rm):
    d = check(rm, positions=[position("GBPUSD", sl=0.0)])
    assert any("no stop-loss" in r for r in d.reasons)
    assert remaining_risk(position(sl=0.0), EUR) == float("inf")
    assert remaining_risk(position(open_=1.1, sl=1.1010), EUR) == 0.0


def test_margin_checks(rm):
    d = check(rm, acct=account(10_000, margin=1_950))             # 19.5% used already
    assert any("margin usage" in r for r in d.reasons)
    low = check(rm, acct=account(10_000, margin=9_990))
    assert any("insufficient free margin" in r for r in low.reasons)
    assert any("disabled" in r for r in check(rm, acct=account(trade_allowed=False)).reasons)


def test_min_lot_too_big_for_budget(rm):
    d = check(rm, acct=account(1_000))
    assert not d.approved and any("minimum lot" in r for r in d.reasons)


def test_risk_scale_only_reduces(rm):
    assert rm.risk_scale(10_000, 0.9, 0.55) == (1.0, "")
    low_conf, why = rm.risk_scale(10_000, 0.55, 0.55)
    assert low_conf == pytest.approx(0.6) and "confidence" in why
    news, why2 = rm.risk_scale(10_000, 0.9, 0.55, news_factor=0.5)
    assert news == 0.5 and "pre-news" in why2
    rm.update(account(9_800), NOW)                                # 2% drawdown of a 5% limit
    dd, why3 = rm.risk_scale(9_800, 0.9, 0.55)
    assert dd == pytest.approx(0.8) and "drawdown" in why3
    for conf in (0.1, 0.5, 0.99):
        assert 0 < rm.risk_scale(9_800, conf, 0.55, 0.5)[0] <= 1.0
    scaled = check(rm, acct=account(9_800), scale=0.5, scale_reason="x")
    assert scaled.risk_pct <= 0.125 + 1e-9


# ------------------------------------------------------------ circuit breakers
def test_daily_loss_limit_halts_until_next_day(rm):
    events = rm.update(account(9_899), NOW + timedelta(hours=1))
    assert [e for e, _ in events] == ["daily_loss_limit"]
    assert rm.update(account(9_890), NOW + timedelta(hours=2)) == []          # reported once
    ok, why = rm.can_open(NOW + timedelta(hours=2))
    assert not ok and "daily loss limit reached" in why
    assert not check(rm, acct=account(9_890)).approved
    next_day = NOW + timedelta(days=1)
    rm.update(account(9_890), next_day)
    assert rm.can_open(next_day)[0] is True


def test_weekly_loss_limit(rm):
    t = NOW
    for day, eq in enumerate((9_920, 9_840, 9_760)):               # three -0.8% days
        t = NOW + timedelta(days=day)
        events = rm.update(account(eq), t + timedelta(hours=5))
    assert "weekly_loss_limit" not in [e for e, _ in events]
    events = rm.update(account(9_745), t + timedelta(hours=6))     # -2.55% on the week
    assert "weekly_loss_limit" in [e for e, _ in events]
    assert "weekly loss limit reached" in rm.can_open(t + timedelta(hours=7))[1]
    monday = NOW + timedelta(days=5)
    rm.update(account(9_745), monday)
    assert rm.can_open(monday)[0] is True


def test_max_drawdown_suspends_until_manual_resume(db):
    rm = RiskManager(RiskLimits(), db)
    rm.update(account(10_000), NOW)
    rm.update(account(10_400), NOW + timedelta(days=3))            # new equity peak
    t = NOW + timedelta(days=20)
    events = rm.update(account(9_870), t)                          # -5.1% from the 10.4k peak
    assert "max_drawdown" in [e for e, _ in events]
    assert rm.state.suspended
    # persists across restarts and does NOT clear on a new day/week
    again = RiskManager(RiskLimits(), db)
    later = t + timedelta(days=30)
    again.update(account(9_900), later)
    ok, why = again.can_open(later)
    assert not ok and any("suspended" in w for w in why)
    again.resume("operator", account(9_900))
    assert again.can_open(later)[0] is True and again.state.peak_equity == 9_900
    assert any(e["event"] == "trading_resumed" and e["actor"] == "operator" for e in db.audit_entries())


def test_loss_streak_cooldown(rm):
    loss = ClosedTrade(1, "EURUSD", Side.BUY, 0.04, 1.1, 1.095, NOW, NOW, profit=-20.0, risk_amount=20.0)
    win = replace(loss, profit=30.0)
    for _ in range(3):
        assert rm.on_trade_closed(loss, NOW) is None
    assert rm.on_trade_closed(win, NOW) is None                    # streak resets
    for _ in range(3):
        rm.on_trade_closed(loss, NOW)
    msg = rm.on_trade_closed(loss, NOW)
    assert msg and "cooling down" in msg
    assert rm.can_open(NOW + timedelta(minutes=30))[0] is False
    assert rm.can_open(NOW + timedelta(minutes=241))[0] is True


def test_order_error_breaker_suspends(rm):
    for i in range(4):
        assert rm.on_order_error(NOW + timedelta(minutes=i)) is None
    msg = rm.on_order_error(NOW + timedelta(minutes=5), "EURUSD rejected")
    assert msg and "abnormal behaviour" in msg and rm.state.suspended
    # errors spread over many hours do not trip it
    rm2 = RiskManager(RiskLimits(), rm.db)
    rm2.resume("op")
    for i in range(10):
        assert rm2.on_order_error(NOW + timedelta(hours=2 * i)) is None


def test_emergency_stop_and_resume(rm, db):
    rm.emergency_stop("operator pressed STOP", actor="dashboard", now=NOW)
    ok, why = rm.can_open(NOW)
    assert not ok and "emergency stop active" in why[0]
    assert not check(rm).approved
    assert RiskManager(RiskLimits(), db).kill_switch()["active"] is True       # survives restart
    rm.resume("operator")
    assert rm.can_open(NOW)[0] is True
    snap = rm.snapshot(10_000)
    assert snap["kill_switch"]["active"] is False and snap["limits"]["risk_per_trade_pct"] == 0.25


def test_margin_warning_event(rm):
    events = rm.update(account(10_000, margin=5_000), NOW)        # 200% < 300%
    assert "margin_warning" in [e for e, _ in events]


# ---------------------------------------------------------- position manager
def test_breakeven_trailing_and_time_stop():
    m = ManagementPlan(breakeven_at_r=1.0, trail_atr_mult=2.0, trail_start_r=1.5, max_bars=10, atr=0.0010)
    args = dict(side_sign=1, entry=1.1000, sl_initial=1.0950, mgmt=m, point=0.00001)
    assert plan(sl_current=1.0950, price=1.1040, bars_held=1, **args).new_sl is None        # 0.8R
    be = plan(sl_current=1.0950, price=1.1052, bars_held=1, **args)
    assert be.new_sl == pytest.approx(1.10002) and be.reason == "breakeven"
    tr = plan(sl_current=1.10002, price=1.1100, bars_held=2, **args)                         # 2R
    assert tr.new_sl == pytest.approx(1.1080) and tr.reason == "trailing"
    # price falls back: the stop is never loosened
    assert plan(sl_current=1.1080, price=1.1085, bars_held=3, **args).new_sl is None
    assert plan(sl_current=1.1080, price=1.1100, bars_held=10, **args).close
    # short side mirrors
    s = plan(side_sign=-1, entry=1.1000, sl_current=1.1050, sl_initial=1.1050, price=1.0945, mgmt=m,
             bars_held=1, point=0.00001)
    assert s.new_sl == pytest.approx(1.09998)
    assert plan(side_sign=1, entry=1.1, sl_current=1.095, sl_initial=1.095, price=1.2, mgmt=ManagementPlan(),
                bars_held=999).new_sl is None
