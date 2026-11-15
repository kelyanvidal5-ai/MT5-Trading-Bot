"""SQLite persistence: orders, trades, decisions, equity curve, risk state,
heartbeats and a hash-chained audit log.

SQLite in WAL mode is used on purpose: one writer (the engine), several
readers (dashboard, reports), zero administration, and the file survives a
crash. Every write is a short transaction, so a kill -9 cannot leave a
half-written order record.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from ..core.models import UTC, ClosedTrade, Side, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY,          -- idempotency key: one row per trading decision
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    volume REAL NOT NULL,
    price REAL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,                -- submitting|filled|placed|rejected|failed|unknown|cancelled
    retcode INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    order_ticket INTEGER DEFAULT 0,
    position_ticket INTEGER DEFAULT 0,
    fill_price REAL DEFAULT 0,
    risk_amount REAL DEFAULT 0,
    management TEXT DEFAULT '{}',
    signal_time TEXT,
    timeframe TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_pos ON orders(position_ticket);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    position_ticket INTEGER NOT NULL,
    client_id TEXT DEFAULT '',
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    price_open REAL NOT NULL,
    price_close REAL NOT NULL,
    time_open TEXT NOT NULL,
    time_close TEXT NOT NULL,
    profit REAL NOT NULL,
    commission REAL DEFAULT 0,
    swap REAL DEFAULT 0,
    sl REAL DEFAULT 0,
    tp REAL DEFAULT 0,
    risk_amount REAL DEFAULT 0,
    r_multiple REAL DEFAULT 0,
    strategy TEXT DEFAULT '',
    exit_reason TEXT DEFAULT '',
    UNIQUE(mode, position_ticket)
);
CREATE INDEX IF NOT EXISTS idx_trades_close ON trades(time_close);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT DEFAULT '',
    action TEXT NOT NULL,                -- executed|rejected|skipped
    confidence REAL DEFAULT 0,
    regime TEXT DEFAULT '',
    explanation TEXT DEFAULT '',
    details TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(time);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    time TEXT PRIMARY KEY, mode TEXT NOT NULL, balance REAL NOT NULL, equity REAL NOT NULL,
    margin REAL DEFAULT 0, margin_free REAL DEFAULT 0, open_positions INTEGER DEFAULT 0,
    open_risk REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL,
    prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY, time TEXT NOT NULL, status TEXT NOT NULL, details TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS strategy_status (
    strategy TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,   -- validated|rejected|untested|suspended
    updated_at TEXT NOT NULL, report TEXT DEFAULT '{}',
    PRIMARY KEY(strategy, symbol)
);
CREATE TABLE IF NOT EXISTS news_events (
    id TEXT PRIMARY KEY, time TEXT NOT NULL, currency TEXT NOT NULL, title TEXT NOT NULL,
    impact TEXT NOT NULL, source TEXT NOT NULL, confirmed INTEGER NOT NULL,
    actual TEXT DEFAULT '', forecast TEXT DEFAULT '', previous TEXT DEFAULT '', fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, level TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL, delivered TEXT DEFAULT ''
);
"""

_GENESIS = "0" * 64


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ kv
    def set_state(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO kv_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), _iso(utcnow())),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv_state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    # -------------------------------------------------------------- orders
    def reserve_order(self, *, client_id: str, mode: str, symbol: str, side: str, order_type: str,
                      volume: float, price: Optional[float], sl: float, tp: float, strategy: str,
                      risk_amount: float, management: dict, signal_time: Optional[datetime],
                      timeframe: str = "") -> bool:
        """Atomically claim an idempotency key *before* anything is sent to the
        broker. Returns False if the key already exists (duplicate decision)."""
        now = _iso(utcnow())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO orders(client_id,created_at,updated_at,mode,symbol,side,order_type,volume,"
                    "price,sl,tp,strategy,status,risk_amount,management,signal_time,timeframe) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (client_id, now, now, mode, symbol, side, order_type, volume, price, sl, tp, strategy,
                     "submitting", risk_amount, json.dumps(management), _iso(signal_time), timeframe),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def update_order(self, client_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _iso(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE orders SET {cols} WHERE client_id=?", (*fields.values(), client_id))

    def get_order(self, client_id: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM orders WHERE client_id=?", (client_id,))
        return rows[0] if rows else None

    def orders_by_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return self.query(f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY created_at", statuses)

    def order_for_position(self, position_ticket: int) -> Optional[dict]:
        rows = self.query("SELECT * FROM orders WHERE position_ticket=? ORDER BY created_at DESC LIMIT 1",
                          (position_ticket,))
        return rows[0] if rows else None

    def count_orders_since(self, since: datetime, statuses: tuple[str, ...] = ("filled", "placed")) -> int:
        """Orders whose SIGNAL time (broker clock) is at or after ``since``."""
        marks = ",".join("?" * len(statuses))
        rows = self.query(
            f"SELECT COUNT(*) AS n FROM orders WHERE signal_time>=? AND status IN ({marks})",
            (_iso(since), *statuses))
        return int(rows[0]["n"])

    # -------------------------------------------------------------- trades
    def record_trade(self, trade: ClosedTrade, mode: str) -> bool:
        """Insert a closed trade once. Returns False if it was already recorded."""
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO trades(mode,position_ticket,client_id,symbol,side,volume,price_open,price_close,"
                    "time_open,time_close,profit,commission,swap,sl,tp,risk_amount,r_multiple,strategy,exit_reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mode, trade.position_ticket, trade.client_id, trade.symbol, trade.side.value, trade.volume,
                     trade.price_open, trade.price_close, _iso(trade.time_open), _iso(trade.time_close),
                     trade.profit, trade.commission, trade.swap, trade.sl, trade.tp, trade.risk_amount,
                     trade.r_multiple, trade.strategy, trade.exit_reason),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def trades(self, mode: Optional[str] = None, since: Optional[datetime] = None,
               strategy: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
        sql, params = "SELECT * FROM trades WHERE 1=1", []
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        if since:
            sql += " AND time_close>=?"
            params.append(_iso(since))
        if strategy:
            sql += " AND strategy=?"
            params.append(strategy)
        sql += " ORDER BY time_close DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, tuple(params))

    @staticmethod
    def trade_from_row(r: dict) -> ClosedTrade:
        return ClosedTrade(
            position_ticket=r["position_ticket"], symbol=r["symbol"], side=Side(r["side"]), volume=r["volume"],
            price_open=r["price_open"], price_close=r["price_close"], time_open=_parse(r["time_open"]),
            time_close=_parse(r["time_close"]), profit=r["profit"], commission=r["commission"], swap=r["swap"],
            sl=r["sl"], tp=r["tp"], risk_amount=r["risk_amount"], strategy=r["strategy"],
            exit_reason=r["exit_reason"], client_id=r["client_id"])

    # ----------------------------------------------------------- decisions
    def record_decision(self, *, time: datetime, symbol: str, strategy: str, side: str, action: str,
                        confidence: float, regime: str, explanation: str, details: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO decisions(time,symbol,strategy,side,action,confidence,regime,explanation,details)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (_iso(time), symbol, strategy, side, action, confidence, regime, explanation,
                 json.dumps(details, default=str)),
            )

    def decisions(self, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    # -------------------------------------------------------------- equity
    def snapshot_equity(self, *, time: datetime, mode: str, balance: float, equity: float, margin: float,
                        margin_free: float, open_positions: int, open_risk: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO equity_snapshots VALUES(?,?,?,?,?,?,?,?)",
                (_iso(time), mode, balance, equity, margin, margin_free, open_positions, open_risk),
            )

    def equity_curve(self, mode: Optional[str] = None, since: Optional[datetime] = None,
                     max_points: int = 2000) -> list[dict]:
        sql, params = "SELECT * FROM equity_snapshots WHERE 1=1", []
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        if since:
            sql += " AND time>=?"
            params.append(_iso(since))
        rows = self.query(sql + " ORDER BY time", tuple(params))
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points - 1)] + [rows[-1]]
        return rows

    # --------------------------------------------------------------- audit
    def audit(self, event: str, details: Optional[dict] = None, actor: str = "engine") -> str:
        """Append to the tamper-evident audit log. Each row stores the SHA-256
        of (previous hash + its own content), so edits or deletions break the
        chain and are detected by :meth:`verify_audit_chain`."""
        payload = json.dumps(details or {}, sort_keys=True, default=str)
        now = _iso(utcnow())
        with self.tx() as c:
            row = c.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else _GENESIS
            digest = hashlib.sha256(f"{prev}|{now}|{actor}|{event}|{payload}".encode()).hexdigest()
            c.execute("INSERT INTO audit_log(time,actor,event,details,prev_hash,hash) VALUES(?,?,?,?,?,?)",
                      (now, actor, event, payload, prev, digest))
        return digest

    def verify_audit_chain(self) -> tuple[bool, int]:
        """Returns (ok, first_bad_row_id or 0)."""
        prev = _GENESIS
        for r in self.query("SELECT * FROM audit_log ORDER BY id"):
            expect = hashlib.sha256(
                f"{prev}|{r['time']}|{r['actor']}|{r['event']}|{r['details']}".encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != expect:
                return False, r["id"]
            prev = r["hash"]
        return True, 0

    def audit_entries(self, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------- heartbeats
    def heartbeat(self, component: str, status: str = "ok", details: Optional[dict] = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO heartbeats(component,time,status,details) VALUES(?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET time=excluded.time,status=excluded.status,"
                "details=excluded.details",
                (component, _iso(utcnow()), status, json.dumps(details or {}, default=str)),
            )

    def heartbeats(self) -> dict[str, dict]:
        out = {}
        for r in self.query("SELECT * FROM heartbeats"):
            age = (utcnow() - _parse(r["time"])).total_seconds()
            out[r["component"]] = {"time": r["time"], "status": r["status"], "age_seconds": age,
                                   "details": json.loads(r["details"] or "{}")}
        return out

    # ----------------------------------------------------- strategy status
    def set_strategy_status(self, strategy: str, symbol: str, status: str, report: Optional[dict] = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO strategy_status(strategy,symbol,status,updated_at,report) VALUES(?,?,?,?,?) "
                "ON CONFLICT(strategy,symbol) DO UPDATE SET status=excluded.status,"
                "updated_at=excluded.updated_at,report=excluded.report",
                (strategy, symbol, status, _iso(utcnow()), json.dumps(report or {}, default=str)),
            )

    def strategy_status(self, strategy: str, symbol: str) -> str:
        rows = self.query("SELECT status FROM strategy_status WHERE strategy=? AND symbol=?", (strategy, symbol))
        return rows[0]["status"] if rows else "untested"

    def strategy_statuses(self) -> list[dict]:
        return self.query("SELECT * FROM strategy_status ORDER BY strategy, symbol")

    # ---------------------------------------------------------------- news
    def upsert_news(self, events: list[dict]) -> None:
        now = _iso(utcnow())
        with self.tx() as c:
            for e in events:
                c.execute(
                    "INSERT OR REPLACE INTO news_events(id,time,currency,title,impact,source,confirmed,actual,"
                    "forecast,previous,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (e["id"], e["time"], e["currency"], e["title"], e["impact"], e["source"],
                     int(e.get("confirmed", True)), e.get("actual", ""), e.get("forecast", ""),
                     e.get("previous", ""), now),
                )

    def news_between(self, start: datetime, end: datetime) -> list[dict]:
        return self.query("SELECT * FROM news_events WHERE time>=? AND time<=? ORDER BY time",
                          (_iso(start), _iso(end)))

    # ------------------------------------------------------- notifications
    def log_notification(self, level: str, title: str, body: str, delivered: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO notifications(time,level,title,body,delivered) VALUES(?,?,?,?,?)",
                      (_iso(utcnow()), level, title, body, delivered))

    def recent_notifications(self, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))

    # ----------------------------------------------------------- utilities
    def demo_track_record(self) -> dict:
        rows = self.query("SELECT MIN(time_close) AS first, MAX(time_close) AS last, COUNT(*) AS n,"
                          " COALESCE(SUM(profit),0) AS pnl FROM trades WHERE mode='demo'")
        r = rows[0]
        days = 0
        if r["first"] and r["last"]:
            days = (_parse(r["last"]) - _parse(r["first"])) / timedelta(days=1)
        return {"trades": int(r["n"]), "days": float(days), "net_profit": float(r["pnl"])}
