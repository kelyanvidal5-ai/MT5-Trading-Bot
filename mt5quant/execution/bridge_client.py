"""Broker implementation that talks to the MT5 gateway over HTTP.

Use this when the trading engine runs on Linux/macOS (or simply on another
machine) and the terminal runs on a Windows desktop/VPS.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

import pandas as pd

from ..bridge.codec import auth_headers, decode, encode
from ..core.models import (AccountInfo, ClosedTrade, OrderRequest, OrderResult, PendingOrder, Position,
                           SymbolSpec, Tick)
from .broker import RC_CONNECTION, RC_LIVE_LOCKED, Broker, BrokerError, result_from_retcode

log = logging.getLogger(__name__)

# transport(url, body_bytes, headers, timeout) -> (status_code, json_dict)
Transport = Callable[[str, bytes, dict, float], tuple[int, dict]]


def _requests_transport(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, dict]:
    import requests

    r = requests.post(url, data=body, headers=headers, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": f"non-JSON response ({r.status_code})"}


class BridgeBroker(Broker):
    name = "bridge"

    def __init__(self, url: str, secret: str, timeout: float = 10.0, transport: Optional[Transport] = None) -> None:
        if not secret:
            raise BrokerError("MT5_BRIDGE_SECRET is not set")
        self.url = url.rstrip("/") + "/rpc"
        self._secret = secret
        self.timeout = timeout
        self._transport = transport or _requests_transport
        self._specs: dict[str, SymbolSpec] = {}

    def _call(self, method: str, **params: Any) -> Any:
        body = json.dumps({"method": method, "params": encode(params)}).encode()
        try:
            status, data = self._transport(self.url, body, auth_headers(self._secret, body), self.timeout)
        except Exception as exc:
            raise BrokerError(f"bridge unreachable: {type(exc).__name__}") from exc
        if status == 200:
            return decode(data.get("result"))
        if status == 403:
            raise _Locked(data.get("error", "locked"))
        raise BrokerError(f"bridge error {status}: {data.get('error', 'unknown')}")

    # ------------------------------------------------------------------ read
    def connect(self) -> bool:
        try:
            return bool(self._call("is_connected"))
        except BrokerError as exc:
            log.error("bridge connect failed: %s", exc)
            return False

    def is_connected(self) -> bool:
        try:
            return bool(self._call("is_connected"))
        except BrokerError:
            return False

    def account_info(self) -> AccountInfo:
        return self._call("account_info")

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        spec = self._call("symbol_spec", symbol=symbol)
        self._specs[symbol] = spec
        return spec

    def tick(self, symbol: str) -> Tick:
        return self._call("tick", symbol=symbol)

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return self._call("rates", symbol=symbol, timeframe=timeframe, count=count)

    def positions(self, symbol: Optional[str] = None) -> list[Position]:
        return self._call("positions", symbol=symbol)

    def pending_orders(self, symbol: Optional[str] = None) -> list[PendingOrder]:
        return self._call("pending_orders", symbol=symbol)

    def closed_trades(self, since: datetime) -> list[ClosedTrade]:
        return self._call("closed_trades", since=since.isoformat())

    # ----------------------------------------------------------------- write
    def _trade(self, method: str, **params: Any) -> OrderResult:
        try:
            return self._call(method, **params)
        except _Locked as exc:
            return result_from_retcode(RC_LIVE_LOCKED, str(exc))
        except BrokerError as exc:
            # Network failure mid-request: the gateway may or may not have executed.
            return OrderResult(ok=False, retcode=RC_CONNECTION, message=str(exc), uncertain=True)

    def send_order(self, request: OrderRequest) -> OrderResult:
        return self._trade("send_order", request=request)

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        return self._trade("modify_position", ticket=ticket, sl=sl, tp=tp)

    def close_position(self, ticket: int, deviation_points: int = 20, reason: str = "") -> OrderResult:
        return self._trade("close_position", ticket=ticket, deviation_points=deviation_points, reason=reason)

    def cancel_order(self, ticket: int) -> OrderResult:
        return self._trade("cancel_order", ticket=ticket)


class _Locked(BrokerError):
    pass
