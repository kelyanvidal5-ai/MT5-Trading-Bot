"""Broker abstraction.

Everything above this layer (strategies, risk, engine, dashboard) talks to a
``Broker`` and never to MetaTrader directly. Implementations:

* ``PaperBroker``   - internal simulator (backtests, paper trading)
* ``MT5Broker``     - official MetaTrader5 Python package (Windows only)
* ``BridgeBroker``  - HTTP client for the gateway that runs next to the
                      terminal on a Windows machine/VPS
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd

from ..core.models import (AccountInfo, ClosedTrade, OrderRequest, OrderResult, PendingOrder, Position,
                           SymbolSpec, Tick, utcnow)

# Return codes (aligned with MetaTrader 5 TRADE_RETCODE_* values)
RC_REQUOTE = 10004
RC_REJECT = 10006
RC_CANCEL = 10007
RC_PLACED = 10008
RC_DONE = 10009
RC_DONE_PARTIAL = 10010
RC_ERROR = 10011
RC_TIMEOUT = 10012
RC_INVALID = 10013
RC_INVALID_VOLUME = 10014
RC_INVALID_PRICE = 10015
RC_INVALID_STOPS = 10016
RC_TRADE_DISABLED = 10017
RC_MARKET_CLOSED = 10018
RC_NO_MONEY = 10019
RC_PRICE_CHANGED = 10020
RC_PRICE_OFF = 10021
RC_TOO_MANY_REQUESTS = 10024
RC_NO_CHANGES = 10025
RC_CONNECTION = 10031
RC_POSITION_CLOSED = 10036
RC_LIVE_LOCKED = 90001        # platform-level: live trading gate refused the order

# The broker definitely did NOT execute -> a bounded retry is safe.
RETRYABLE = {RC_REQUOTE, RC_PRICE_CHANGED, RC_PRICE_OFF, RC_TOO_MANY_REQUESTS}
# Execution state unknown -> never resend blindly; reconcile first.
UNCERTAIN = {RC_TIMEOUT, RC_CONNECTION, RC_ERROR}

RETCODE_TEXT = {
    RC_REQUOTE: "requote", RC_REJECT: "request rejected", RC_CANCEL: "cancelled by trader",
    RC_PLACED: "order placed", RC_DONE: "done", RC_DONE_PARTIAL: "partially filled",
    RC_ERROR: "request processing error", RC_TIMEOUT: "request timed out", RC_INVALID: "invalid request",
    RC_INVALID_VOLUME: "invalid volume", RC_INVALID_PRICE: "invalid price", RC_INVALID_STOPS: "invalid stops",
    RC_TRADE_DISABLED: "trade disabled", RC_MARKET_CLOSED: "market closed", RC_NO_MONEY: "not enough money",
    RC_PRICE_CHANGED: "price changed", RC_PRICE_OFF: "no quotes", RC_TOO_MANY_REQUESTS: "too many requests",
    RC_NO_CHANGES: "no changes", RC_CONNECTION: "no connection to trade server",
    RC_POSITION_CLOSED: "position already closed", RC_LIVE_LOCKED: "live trading is locked",
}


class BrokerError(RuntimeError):
    """Connectivity or protocol failure (as opposed to an order rejection)."""


class MarketDataSource(ABC):
    """Read-only market access."""

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    def tick(self, symbol: str) -> Tick: ...

    @abstractmethod
    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Most recent ``count`` bars, indexed by UTC open time. May include
        the still-forming bar; callers drop it with ``drop_incomplete``."""

    def now(self) -> datetime:
        return utcnow()

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        return None


class Broker(MarketDataSource):
    """Market access plus trading."""

    name = "broker"

    @abstractmethod
    def account_info(self) -> AccountInfo: ...

    @abstractmethod
    def positions(self, symbol: Optional[str] = None) -> list[Position]: ...

    @abstractmethod
    def pending_orders(self, symbol: Optional[str] = None) -> list[PendingOrder]: ...

    @abstractmethod
    def send_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult: ...

    @abstractmethod
    def close_position(self, ticket: int, deviation_points: int = 20, reason: str = "") -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, ticket: int) -> OrderResult: ...

    @abstractmethod
    def closed_trades(self, since: datetime) -> list[ClosedTrade]:
        """Completed round trips closed at or after ``since``."""


def result_from_retcode(retcode: int, message: str = "", **kw) -> OrderResult:
    ok = retcode in (RC_DONE, RC_PLACED, RC_DONE_PARTIAL)
    return OrderResult(ok=ok, retcode=retcode, message=message or RETCODE_TEXT.get(retcode, str(retcode)),
                       retryable=retcode in RETRYABLE, uncertain=retcode in UNCERTAIN, **kw)
