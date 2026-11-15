"""In-memory stand-in for the ``MetaTrader5`` module.

It mimics the subset of the official API that the adapter uses (function
names, constants and result field names follow the MetaQuotes documentation)
so the adapter logic can be tested on any OS. It is NOT a substitute for
testing against a real terminal on a demo account.
"""
from __future__ import annotations

import time
from types import SimpleNamespace as NS

import numpy as np


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
    TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = 16385, 16388, 16408, 32769
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
    SYMBOL_TRADE_MODE_FULL = 4
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
    DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPE_BALANCE = 0, 1, 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, demo: bool = True, server_offset_hours: float = 2.0) -> None:
        self.demo = demo
        self.offset = server_offset_hours * 3600
        self.initialized = False
        self.connected = True
        self.init_ok = True
        self.init_kwargs: dict = {}
        self.balance = 10_000.0
        self.prices = {"EURUSD": (1.10000, 1.10010)}
        self.positions: dict[int, NS] = {}
        self.orders: dict[int, NS] = {}
        self.deals: list[NS] = []
        self.sent: list[dict] = []
        self.next_ticket = 500
        self.next_retcode: int | None = None      # force a retcode for the next order_send
        self.return_none = False

    # -- lifecycle
    def initialize(self, *args, **kwargs) -> bool:
        self.init_kwargs = kwargs
        self.initialized = self.init_ok
        return self.init_ok

    def shutdown(self) -> None:
        self.initialized = False

    def last_error(self):
        return (-1, "fake error")

    def terminal_info(self):
        return NS(connected=self.connected, trade_allowed=True) if self.initialized else None

    def _server_now(self) -> float:
        return time.time() + self.offset

    # -- account / symbols
    def account_info(self):
        return NS(login=12345678, server="Fake-Demo" if self.demo else "Fake-Real", currency="USD",
                  balance=self.balance, equity=self.balance, margin=0.0, margin_free=self.balance,
                  margin_level=0.0, leverage=30, profit=0.0, trade_allowed=True, trade_expert=True,
                  trade_mode=self.ACCOUNT_TRADE_MODE_DEMO if self.demo else self.ACCOUNT_TRADE_MODE_REAL)

    def symbol_info(self, symbol):
        if symbol not in self.prices:
            return None
        return NS(digits=5, point=0.00001, trade_tick_size=0.00001, trade_tick_value=1.0,
                  trade_contract_size=100000.0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
                  trade_stops_level=10, currency_base=symbol[:3], currency_profit=symbol[3:6],
                  currency_margin=symbol[:3], visible=True, trade_mode=self.SYMBOL_TRADE_MODE_FULL,
                  filling_mode=2, path=f"Forex\\{symbol}")

    def symbol_select(self, symbol, enable):
        return symbol in self.prices

    def symbol_info_tick(self, symbol):
        if symbol not in self.prices:
            return None
        bid, ask = self.prices[symbol]
        now = self._server_now()
        return NS(time=int(now), time_msc=int(now * 1000), bid=bid, ask=ask)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if symbol not in self.prices:
            return None
        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                 ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")]
        step = {1: 60, 5: 300, 15: 900, 30: 1800, 16385: 3600, 16388: 14400, 16408: 86400, 32769: 604800}[timeframe]
        last = int(self._server_now() // step * step)
        out = np.zeros(count, dtype=dtype)
        for i in range(count):
            t = last - (count - 1 - i) * step
            px = 1.1 + 0.0001 * i
            out[i] = (t, px, px + 0.0005, px - 0.0005, px + 0.0002, 100 + i, 10, 0)
        return out

    def copy_rates_range(self, symbol, timeframe, start, end):
        return self.copy_rates_from_pos(symbol, timeframe, 0, 10)

    # -- trading
    def positions_get(self, symbol=None, ticket=None):
        ps = list(self.positions.values())
        if symbol:
            ps = [p for p in ps if p.symbol == symbol]
        if ticket:
            ps = [p for p in ps if p.ticket == ticket]
        return tuple(ps)

    def orders_get(self, symbol=None):
        return tuple(o for o in self.orders.values() if not symbol or o.symbol == symbol)

    def order_send(self, req: dict):
        self.sent.append(dict(req))
        if self.return_none:
            return None
        if self.next_retcode is not None:
            rc, self.next_retcode = self.next_retcode, None
            return NS(retcode=rc, order=0, deal=0, volume=0.0, price=0.0, comment="forced")
        action = req["action"]
        now = int(self._server_now())
        if action == self.TRADE_ACTION_DEAL and "position" not in req:
            ticket = self.next_ticket
            self.next_ticket += 1
            self.positions[ticket] = NS(ticket=ticket, symbol=req["symbol"], type=req["type"],
                                        volume=req["volume"], price_open=req["price"], sl=req["sl"], tp=req["tp"],
                                        time=now, price_current=req["price"], swap=0.0, profit=0.0,
                                        magic=req["magic"], comment=req["comment"])
            self.deals.append(NS(ticket=ticket + 10_000, position_id=ticket, symbol=req["symbol"],
                                 type=req["type"], entry=self.DEAL_ENTRY_IN, volume=req["volume"],
                                 price=req["price"], profit=0.0, commission=-0.35, swap=0.0, fee=0.0,
                                 time=now, time_msc=now * 1000, magic=req["magic"], comment=req["comment"],
                                 reason=3))
            return NS(retcode=10009, order=ticket, deal=ticket + 10_000, volume=req["volume"],
                      price=req["price"], comment="Request executed")
        if action == self.TRADE_ACTION_DEAL:
            p = self.positions.pop(req["position"], None)
            if p is None:
                return NS(retcode=10036, order=0, deal=0, volume=0.0, price=0.0, comment="closed")
            sign = 1 if p.type == 0 else -1
            profit = (req["price"] - p.price_open) * sign * p.volume * 100000
            self.balance += profit
            self.deals.append(NS(ticket=p.ticket + 20_000, position_id=p.ticket, symbol=p.symbol,
                                 type=req["type"], entry=self.DEAL_ENTRY_OUT, volume=p.volume,
                                 price=req["price"], profit=profit, commission=-0.35, swap=0.0, fee=0.0,
                                 time=now, time_msc=now * 1000, magic=p.magic, comment=req.get("comment", ""),
                                 reason=3))
            return NS(retcode=10009, order=p.ticket + 20_000, deal=p.ticket + 20_000, volume=p.volume,
                      price=req["price"], comment="Request executed")
        if action == self.TRADE_ACTION_SLTP:
            p = self.positions.get(req["position"])
            if p is None:
                return NS(retcode=10036, order=0, deal=0, volume=0.0, price=0.0, comment="closed")
            p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=10009, order=0, deal=0, volume=0.0, price=0.0, comment="ok")
        if action == self.TRADE_ACTION_PENDING:
            ticket = self.next_ticket
            self.next_ticket += 1
            self.orders[ticket] = NS(ticket=ticket, symbol=req["symbol"], type=req["type"],
                                     volume_current=req["volume"], price_open=req["price"], sl=req["sl"],
                                     tp=req["tp"], time_setup=now, magic=req["magic"], comment=req["comment"],
                                     time_expiration=req.get("expiration", 0))
            return NS(retcode=10008, order=ticket, deal=0, volume=req["volume"], price=req["price"],
                      comment="placed")
        if action == self.TRADE_ACTION_REMOVE:
            self.orders.pop(req["order"], None)
            return NS(retcode=10009, order=req["order"], deal=0, volume=0.0, price=0.0, comment="removed")
        return NS(retcode=10013, order=0, deal=0, volume=0.0, price=0.0, comment="invalid")

    def history_deals_get(self, start, end):
        return tuple(self.deals)
