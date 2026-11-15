"""Wire format and request signing for the MT5 bridge.

Requests are JSON-RPC-like POSTs authenticated with HMAC-SHA256 over
``timestamp.nonce.body`` using a shared secret (``MT5_BRIDGE_SECRET``). The
timestamp bounds replay to a short window and the nonce blocks replays inside
that window. The secret itself never travels over the wire.

The gateway binds to 127.0.0.1 by default; reach it through an SSH tunnel,
WireGuard/Tailscale, or a TLS-terminating reverse proxy. HMAC protects
integrity and authenticity, not confidentiality.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd

from ..core.models import (AccountInfo, ClosedTrade, OrderRequest, OrderResult, OrderType, PendingOrder,
                           Position, Side, SymbolSpec, Tick)

_DATACLASSES = {c.__name__: c for c in (AccountInfo, ClosedTrade, OrderRequest, OrderResult, PendingOrder,
                                        Position, SymbolSpec, Tick)}
_ENUM_FIELDS = {"side": Side, "order_type": OrderType}
_TIME_FIELDS = {"time", "time_open", "time_close", "time_setup", "expiry"}


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    msg = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def auth_headers(secret: str, body: bytes) -> dict[str, str]:
    ts, nonce = str(int(time.time())), secrets.token_hex(12)
    return {"X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sign(secret, ts, nonce, body),
            "Content-Type": "application/json"}


def verify(secret: str, timestamp: str, nonce: str, signature: str, body: bytes, max_skew: int) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > max_skew:
            return False
    except (TypeError, ValueError):
        return False
    if not nonce or not signature:
        return False
    return hmac.compare_digest(sign(secret, timestamp, nonce, body), signature)


def encode(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        d = {f.name: encode(getattr(obj, f.name)) for f in fields(obj)}
        d["__type__"] = type(obj).__name__
        return d
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, pd.DataFrame):
        return {"__type__": "DataFrame",
                "index": [int(t.value // 10 ** 6) for t in obj.index],
                "columns": {c: [float(x) for x in obj[c].values] for c in obj.columns}}
    if isinstance(obj, (list, tuple)):
        return [encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: encode(v) for k, v in obj.items()}
    return obj


def decode(obj: Any) -> Any:
    if isinstance(obj, list):
        return [decode(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    kind = obj.get("__type__")
    if kind == "DataFrame":
        idx = pd.to_datetime(obj["index"], unit="ms", utc=True)
        return pd.DataFrame(obj["columns"], index=pd.DatetimeIndex(idx))
    if kind in _DATACLASSES:
        cls = _DATACLASSES[kind]
        names = {f.name for f in fields(cls)}
        kwargs = {}
        for k, v in obj.items():
            if k not in names:
                continue
            if k in _ENUM_FIELDS and v is not None:
                v = _ENUM_FIELDS[k](v)
            elif k in _TIME_FIELDS and isinstance(v, str):
                v = datetime.fromisoformat(v)
            else:
                v = decode(v)
            kwargs[k] = v
        return cls(**kwargs)
    return {k: decode(v) for k, v in obj.items()}
