"""MT5 gateway: runs on the Windows machine/VPS next to the terminal and
exposes a ``Broker`` to the trading engine over authenticated HTTP.

Only a fixed whitelist of broker methods is reachable. Order submission is
idempotent on the server side as well: a repeated ``client_id`` returns the
cached result instead of executing twice (covers client timeouts/retries).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..execution.broker import Broker, BrokerError
from ..execution.live_gate import LiveTradingLocked
from .codec import decode, encode, verify

log = logging.getLogger(__name__)

READ_METHODS = {"is_connected", "account_info", "symbol_spec", "tick", "rates", "positions",
                "pending_orders", "closed_trades", "now"}
WRITE_METHODS = {"send_order", "modify_position", "close_position", "cancel_order"}


def create_gateway_app(broker: Broker, secret: str, max_skew: int = 30) -> FastAPI:
    if not secret or len(secret) < 32:
        raise ValueError("MT5_BRIDGE_SECRET must be set to a random string of at least 32 characters")
    app = FastAPI(title="MT5 Quant gateway", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()                    # the MT5 IPC API is not thread-safe
    nonces: OrderedDict[str, float] = OrderedDict()
    sent: OrderedDict[str, dict] = OrderedDict()

    def _seen(nonce: str) -> bool:
        now = time.time()
        while nonces and next(iter(nonces.values())) < now - 2 * max_skew:
            nonces.popitem(last=False)
        if nonce in nonces:
            return True
        nonces[nonce] = now
        return False

    @app.get("/health")
    def health() -> dict:
        # Deliberately unauthenticated and free of account details.
        return {"status": "ok"}

    @app.post("/rpc")
    async def rpc(request: Request) -> JSONResponse:
        body = await request.body()
        h = request.headers
        if not verify(secret, h.get("x-timestamp", ""), h.get("x-nonce", ""), h.get("x-signature", ""),
                      body, max_skew):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if _seen(h.get("x-nonce", "")):
            return JSONResponse({"error": "replayed request"}, status_code=401)
        try:
            payload = json.loads(body)
            method, params = payload["method"], decode(payload.get("params", {}))
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "malformed request"}, status_code=400)
        if method not in READ_METHODS | WRITE_METHODS or not isinstance(params, dict):
            return JSONResponse({"error": f"method not allowed: {method}"}, status_code=400)
        with lock:
            try:
                if method == "send_order":
                    req = params["request"]
                    if req.client_id in sent:
                        log.warning("duplicate client_id %s - returning cached result", req.client_id)
                        return JSONResponse({"result": sent[req.client_id], "duplicate": True})
                    result = encode(broker.send_order(req))
                    sent[req.client_id] = result
                    while len(sent) > 5000:
                        sent.popitem(last=False)
                    return JSONResponse({"result": result})
                if method == "closed_trades":
                    params["since"] = datetime.fromisoformat(params["since"]) \
                        if isinstance(params.get("since"), str) else params["since"]
                result = getattr(broker, method)(**params)
                return JSONResponse({"result": encode(result)})
            except LiveTradingLocked as exc:
                return JSONResponse({"error": str(exc), "kind": "locked"}, status_code=403)
            except BrokerError as exc:
                return JSONResponse({"error": str(exc), "kind": "broker"}, status_code=503)
            except (TypeError, KeyError, ValueError) as exc:
                return JSONResponse({"error": f"bad parameters: {exc}"}, status_code=400)
            except Exception:
                log.exception("gateway internal error in %s", method)
                return JSONResponse({"error": "internal error"}, status_code=500)

    return app
