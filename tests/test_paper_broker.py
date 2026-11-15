from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.backtesting.costs import CostModel
from mt5quant.core.models import OrderRequest, OrderType, Side
from mt5quant.execution import broker as B
from mt5quant.execution.paper import PaperBroker

T0 = datetime(2024, 3, 13, 12, 0, tzinfo=timezone.utc)


def make(slip=0.0, commission=3.5, **kw) -> PaperBroker:
    cost = CostModel(slippage_points_mean=slip, execution_delay_ms=0, commission_per_lot_per_side=commission,
                     swap_long_points=-5, swap_short_points=-5)
    b = PaperBroker(balance=10_000, cost=cost, **kw)
    b.set_price("EURUSD", 1.10000, 1.10010, T0)
    return b


def order(side=Side.BUY, volume=0.10, sl=1.0950, tp=1.1100, **kw) -> OrderRequest:
    return OrderRequest(client_id="c1", symbol="EURUSD", side=side, volume=volume, sl=sl, tp=tp,
                        strategy="test", **kw)


def test_market_buy_fills_at_ask_and_charges_commission():
    b = make()
    r = b.send_order(order())
    assert r.ok and r.retcode == B.RC_DONE and r.fill_price == 1.10010
    acct = b.account_info()
    assert acct.balance == pytest.approx(10_000 - 0.35)
    # floating = (bid - ask) * 0.1 lot * 100k = -1.0
    assert acct.equity == pytest.approx(10_000 - 0.35 - 1.0)
    assert acct.margin == pytest.approx(0.1 * 100_000 * 1.10010 / 30, rel=1e-6)


def test_stop_loss_hit_inside_bar_and_r_multiple():
    b = make()
    b.send_order(order())
    b.process_bar("EURUSD", T0, 1.1000, 1.1005, 1.0940, 1.0945, 0.0001, T0 + timedelta(minutes=5))
    assert not b.positions()
    t = b.all_closed_trades[0]
    assert t.exit_reason == "sl" and t.price_close == 1.0950
    gross = (1.0950 - 1.10010) * 0.1 * 100_000
    assert t.profit == pytest.approx(gross - 0.70)
    assert t.risk_amount == pytest.approx(51.0)
    assert t.r_multiple == pytest.approx((gross - 0.70) / 51.0)
    assert b.balance == pytest.approx(10_000 + gross - 0.70)


def test_stop_and_target_in_same_bar_assumes_stop_first():
    b = make()
    b.send_order(order(sl=1.0990, tp=1.1020))
    b.process_bar("EURUSD", T0, 1.1000, 1.1030, 1.0980, 1.1010, 0.0001, T0 + timedelta(minutes=5))
    assert b.all_closed_trades[0].exit_reason == "sl"


def test_gap_through_stop_fills_at_open_not_at_stop():
    b = make()
    b.send_order(order(sl=1.0990))
    b.process_bar("EURUSD", T0, 1.0970, 1.0975, 1.0960, 1.0965, 0.0001, T0 + timedelta(minutes=5))
    assert b.all_closed_trades[0].price_close == 1.0970


def test_short_closes_on_ask():
    b = make()
    r = b.send_order(order(side=Side.SELL, sl=1.1020, tp=1.0950))
    assert r.fill_price == 1.10000
    # bid high 1.1018 + spread 0.0003 = ask 1.1021 >= SL
    b.process_bar("EURUSD", T0, 1.1000, 1.1018, 1.0995, 1.1010, 0.0003, T0 + timedelta(minutes=5))
    t = b.all_closed_trades[0]
    assert t.exit_reason == "sl" and t.price_close == 1.1020
    b.send_order(order(side=Side.SELL, sl=1.1060, tp=1.0950))
    # target: ask = low + spread must reach TP
    b.process_bar("EURUSD", T0, 1.1000, 1.1005, 1.0949, 1.0960, 0.0003, T0 + timedelta(minutes=10))
    assert b.positions()                                  # 1.0949+0.0003 > 1.0950 -> not hit
    b.process_bar("EURUSD", T0, 1.0960, 1.0962, 1.0946, 1.0950, 0.0003, T0 + timedelta(minutes=15))
    assert b.all_closed_trades[-1].exit_reason == "tp"


def test_take_profit_never_gets_positive_slippage():
    b = make(slip=5.0)
    b.send_order(order(tp=1.1050, deviation_points=1000))
    b.process_bar("EURUSD", T0, 1.1000, 1.1090, 1.0999, 1.1080, 0.0001, T0 + timedelta(minutes=5))
    assert b.all_closed_trades[0].price_close == 1.1050


def test_slippage_is_adverse():
    b = make(slip=5.0)
    r = b.send_order(order(deviation_points=1000))
    assert r.fill_price >= 1.10010
    b2 = make(slip=5.0)
    r2 = b2.send_order(order(side=Side.SELL, sl=1.1050, tp=1.0900, deviation_points=1000))
    assert r2.fill_price <= 1.10000


def test_requote_when_slippage_exceeds_deviation():
    b = make(slip=500.0)
    r = b.send_order(order(deviation_points=1))
    assert not r.ok and r.retcode == B.RC_REQUOTE and r.retryable


@pytest.mark.parametrize("kw,code", [
    (dict(volume=0.001), B.RC_INVALID_VOLUME),
    (dict(volume=0.015), B.RC_INVALID_VOLUME),
    (dict(volume=1000.0), B.RC_INVALID_VOLUME),
    (dict(sl=1.1050), B.RC_INVALID_STOPS),               # stop above price for a buy
    (dict(tp=1.0900), B.RC_INVALID_STOPS),               # target below price for a buy
    (dict(volume=50.0), B.RC_NO_MONEY),
])
def test_rejections_use_mt5_retcodes(kw, code):
    r = make().send_order(order(**kw))
    assert not r.ok and r.retcode == code


def test_trade_disabled_and_no_quotes():
    b = make()
    b.trade_allowed = False
    assert b.send_order(order()).retcode == B.RC_TRADE_DISABLED
    b2 = PaperBroker()
    r = b2.send_order(order())
    assert r.retcode == B.RC_MARKET_CLOSED


def test_modify_and_manual_close():
    b = make()
    r = b.send_order(order())
    assert b.modify_position(r.position_ticket, 1.0980, 1.1100).ok
    assert b.positions()[0].sl == 1.0980
    assert b.modify_position(r.position_ticket, 1.1050, 1.1100).retcode == B.RC_INVALID_STOPS
    assert b.modify_position(r.position_ticket, 1.0980, 1.1100).retcode == B.RC_NO_CHANGES
    assert b.close_position(r.position_ticket).ok
    assert b.close_position(r.position_ticket).retcode == B.RC_POSITION_CLOSED
    assert b.all_closed_trades[0].exit_reason == "manual"


def test_pending_orders():
    b = make()
    exp = T0 + timedelta(minutes=15)
    r = b.send_order(order(order_type=OrderType.LIMIT, price=1.0980, sl=1.0950, tp=1.1050, expiry=exp))
    assert r.ok and r.retcode == B.RC_PLACED and len(b.pending_orders()) == 1
    assert b.send_order(order(order_type=OrderType.LIMIT, price=1.1050)).retcode == B.RC_INVALID_PRICE
    # ask low = 1.0978 + 0.0001 <= 1.0980 -> filled at limit price
    b.process_bar("EURUSD", T0, 1.1000, 1.1002, 1.0978, 1.0985, 0.0001, T0 + timedelta(minutes=5))
    assert not b.pending_orders() and b.positions()[0].price_open == 1.0980
    # buy stop
    b2 = make()
    b2.send_order(order(order_type=OrderType.STOP, price=1.1020, sl=1.0990, tp=1.1080))
    b2.process_bar("EURUSD", T0, 1.1000, 1.1025, 1.0998, 1.1022, 0.0001, T0 + timedelta(minutes=5))
    assert b2.positions()[0].price_open == pytest.approx(1.1020)
    # expiry
    b3 = make()
    b3.send_order(order(order_type=OrderType.LIMIT, price=1.0900, sl=1.0850, tp=1.1000, expiry=exp))
    b3.process_bar("EURUSD", exp, 1.1000, 1.1002, 1.0990, 1.1000, 0.0001, exp + timedelta(minutes=5))
    assert not b3.pending_orders() and not b3.positions()
    assert b.cancel_order(999).retcode == B.RC_INVALID


def test_swap_charged_at_rollover_triple_on_wednesday():
    b = make()                       # T0 is a Wednesday 12:00 UTC
    b.send_order(order(sl=1.05, tp=1.2))
    b.set_price("EURUSD", 1.1000, 1.1001, T0 + timedelta(hours=11))    # crosses 22:00 UTC Wednesday
    pos = b.positions()[0]
    # -5 points * 0.00001 * 100000 * 0.1 lot * 3 = -1.5
    assert pos.swap == pytest.approx(-1.5)
    b.set_price("EURUSD", 1.1000, 1.1001, T0 + timedelta(hours=35))    # Thursday rollover
    assert b.positions()[0].swap == pytest.approx(-2.0)


def test_jpy_pair_pnl_converted_to_account_currency():
    b = PaperBroker(balance=10_000, cost=CostModel(slippage_points_mean=0, execution_delay_ms=0,
                                                   commission_per_lot_per_side=0))
    b.set_price("USDJPY", 150.000, 150.010, T0)
    spec = b.symbol_spec("USDJPY")
    assert spec.tick_value == pytest.approx(0.001 * 100_000 / 150.005, rel=1e-6)
    b.send_order(OrderRequest("c", "USDJPY", Side.BUY, 0.1, sl=149.0, tp=152.0))
    b.set_price("USDJPY", 151.010, 151.020, T0 + timedelta(minutes=1))
    # +1.000 JPY * 10k units = 10000 JPY ~= 66.2 USD
    assert b.account_info().profit == pytest.approx(10_000 / 151.015, rel=1e-3)


def test_state_roundtrip_restores_positions_and_balance():
    saved = {}
    b = make(on_change=lambda s: saved.update(s))
    b.send_order(order())
    b2 = PaperBroker(balance=1.0)
    b2.load_state(saved)
    b2.set_price("EURUSD", 1.10000, 1.10010, T0)
    assert b2.balance == b.balance and len(b2.positions()) == 1
    assert b2.send_order(order()).position_ticket != b.positions()[0].ticket


def test_tick_triggers_stop():
    b = make()
    b.send_order(order(sl=1.0990))
    b.set_price("EURUSD", 1.0985, 1.0986, T0 + timedelta(seconds=30))
    assert b.all_closed_trades[0].price_close == 1.0985
