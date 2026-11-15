"""Dashboard web application (FastAPI + a static, dependency-free front end).

The dashboard is a read-only view of the database plus two audited controls:
EMERGENCY STOP and RESUME. It never talks to the broker itself - the STOP
button raises the persistent kill switch, which the engine honours on its
next cycle (block entries, cancel orders, close positions).

Security model
* Binds to 127.0.0.1 by default.
* If ``DASHBOARD_TOKEN`` is set, every API call needs it.
* Binding to a non-loopback address WITHOUT a token is refused at startup.
* RESUME always needs the token. STOP needs the token, or - when no token
  is configured - a loopback client (stopping only ever reduces risk).
* Strict Content-Security-Policy: no inline script, no third-party origins.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.config import AppConfig, Secrets
from ..core.models import UTC
from ..monitoring import reports, stats
from ..risk_management.manager import RiskManager
from ..strategies.registry import STRATEGIES

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
RANGES = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _clean(obj):
    """JSON cannot carry inf/nan."""
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def create_app(config: AppConfig, db) -> FastAPI:
    token = Secrets.get("DASHBOARD_TOKEN")
    if not token and not _is_loopback(config.dashboard.host):
        raise RuntimeError("refusing to expose the dashboard on a non-loopback address without DASHBOARD_TOKEN")
    app = FastAPI(title="MT5 Quant dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    mode = config.mode.value

    def authed(request: Request) -> bool:
        supplied = request.headers.get("x-dashboard-token", "")
        return bool(token) and hmac.compare_digest(supplied.encode(), token.encode())

    def guard_read(request: Request) -> None:
        if token and not authed(request):
            raise HTTPException(401, "dashboard token required")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def engine_now() -> datetime:
        beat = db.heartbeats().get("engine")
        raw = (beat or {}).get("details", {}).get("engine_time")
        return datetime.fromisoformat(raw) if raw else datetime.now(UTC)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def meta() -> dict:
        return {"token_required": bool(token), "mode": mode}

    @app.get("/api/state")
    def state(request: Request) -> JSONResponse:
        guard_read(request)
        health = reports.health(db, max_age_seconds=max(60.0, 6 * config.engine.loop_interval_seconds))
        details = health.get("details") or {}
        now = engine_now()
        risk = RiskManager(config.risk, db).snapshot()
        summary = stats.summary(db, mode, now)
        account = details.get("account")
        positions = db.get_state("positions", []) if health["alive"] else db.get_state("positions", [])
        open_risk = sum(p["risk"] or 0.0 for p in positions)
        equity = account["equity"] if account else (summary["last_equity"] or 0.0)
        statuses = {(s["strategy"], s["symbol"]): s for s in db.strategy_statuses()}
        per = {r["strategy"]: r for r in stats.by_strategy(db, mode)}
        strategies = []
        for name in config.strategies.enabled:
            cls = STRATEGIES.get(name)
            st = sorted({v["status"] for (n, _), v in statuses.items() if n == name}) or ["untested"]
            row = per.get(name, {})
            strategies.append({
                "name": name, "style": cls.style if cls else "", "timeframe": cls.entry_tf if cls else "",
                "status": "/".join(st), "trades": row.get("trades", 0), "win_rate": row.get("win_rate"),
                "net_profit": row.get("net_profit", 0.0), "expectancy_r": row.get("expectancy_r"),
                "profit_factor": row.get("profit_factor"),
                "health": (db.get_state(f"health_{name}") or {}).get("status", "insufficient_data")})
        news = []
        for e in db.news_between(now - timedelta(hours=1), now + timedelta(hours=48)):
            if e["impact"] in ("high", "medium"):
                news.append({k: e[k] for k in ("time", "currency", "title", "impact", "source", "confirmed")})
        ok, bad = db.verify_audit_chain()
        return JSONResponse(_clean({
            "mode": mode, "server_time": datetime.now(UTC).isoformat(), "engine_time": now.isoformat(),
            "health": {"alive": health["alive"], "status": health["status"], "age_seconds": health["age_seconds"]},
            "connection": {"connected": health["alive"] and health["status"] == "running",
                           "broker": details.get("broker"), "cycle_ms": details.get("cycle_ms"),
                           "synthetic_data": details.get("synthetic_data", False)},
            "markets": details.get("markets", {}), "sessions": details.get("sessions", []),
            "can_open": details.get("can_open"), "blocks": details.get("blocks", []),
            "account": account, "summary": summary, "risk": risk,
            "exposure": {"open_risk": open_risk, "open_risk_pct": open_risk / equity * 100 if equity else 0.0,
                         "positions": len(positions),
                         "max_positions": config.risk.max_open_positions,
                         "max_open_risk_pct": config.risk.max_total_open_risk_pct},
            "positions": positions, "strategies": strategies, "news": news[:20],
            "notifications": db.recent_notifications(12),
            "audit_ok": ok, "audit_bad_row": bad,
        }))

    @app.get("/api/equity")
    def equity(request: Request, range: str = "7d") -> JSONResponse:
        guard_read(request)
        if range not in RANGES:
            raise HTTPException(400, "range must be one of " + ", ".join(RANGES))
        days = RANGES[range]
        since = engine_now() - timedelta(days=days) if days else None
        curve = db.equity_curve(mode=mode, since=since, max_points=600)
        return JSONResponse(_clean({
            "range": range,
            "points": [{"t": p["time"], "equity": p["equity"], "balance": p["balance"]} for p in curve],
            "daily": stats.daily_pnl(db, mode, days=min(days or 60, 60), now=engine_now())}))

    @app.get("/api/trades")
    def trades(request: Request, limit: int = 50) -> JSONResponse:
        guard_read(request)
        rows = db.trades(mode=mode, limit=max(1, min(limit, 500)))
        keep = ("time_open", "time_close", "symbol", "side", "volume", "price_open", "price_close", "profit",
                "r_multiple", "strategy", "exit_reason", "commission", "swap")
        return JSONResponse(_clean([{k: r[k] for k in keep} for r in rows]))

    @app.get("/api/decisions")
    def decisions(request: Request, limit: int = 30) -> JSONResponse:
        guard_read(request)
        rows = db.decisions(max(1, min(limit, 200)))
        return JSONResponse(_clean([{k: r[k] for k in ("time", "symbol", "strategy", "side", "action",
                                                         "confidence", "regime", "explanation")} for r in rows]))

    async def _body(request: Request) -> dict:
        try:
            data = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "invalid body")
        return data

    @app.post("/api/emergency-stop")
    async def emergency_stop(request: Request) -> dict:
        client = request.client.host if request.client else None
        if not (authed(request) or (not token and _is_loopback(client))):
            raise HTTPException(401, "dashboard token required")
        data = await _body(request)
        reason = str(data.get("reason") or "operator pressed EMERGENCY STOP")[:200]
        RiskManager(config.risk, db).emergency_stop(reason, actor="dashboard")
        return {"ok": True, "message": "Emergency stop set. The engine blocks entries and closes positions "
                                       "on its next cycle."}

    @app.post("/api/resume")
    async def resume(request: Request) -> dict:
        if not token:
            raise HTTPException(403, "set DASHBOARD_TOKEN to enable resume from the dashboard "
                                     "(or use: python main.py resume --confirm)")
        if not authed(request):
            raise HTTPException(401, "dashboard token required")
        data = await _body(request)
        if data.get("confirm") is not True:
            raise HTTPException(400, "confirmation required")
        RiskManager(config.risk, db).resume(actor="dashboard")
        return {"ok": True, "message": "Trading resumed."}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve_in_thread(config: AppConfig, db) -> threading.Thread:
    """Run the dashboard next to the engine (daemon thread)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(create_app(config, db), host=config.dashboard.host,
                                           port=config.dashboard.port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="dashboard", daemon=True)
    thread.start()
    return thread
