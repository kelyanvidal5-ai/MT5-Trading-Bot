"""Order lifecycle with duplicate-order prevention.

Guarantee: one trading decision can produce at most one order, even across
crashes, restarts, timeouts and retries.

How:
1. Each decision gets a deterministic ``client_id`` derived from
   (strategy, symbol, timeframe, signal bar time, side).
2. The id is written to the database (unique key) BEFORE anything is sent.
   If the row already exists the decision was already acted on -> skip.
3. The id travels to the broker in the order comment. After a timeout or a
   crash the engine never resends; it looks for a position/order/deal
   carrying that comment and settles the row from what it finds.
4. Only return codes that mean "definitely not executed" are retried, a
   bounded number of times.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..core.models import ClosedTrade, OrderRequest, OrderResult, Position, Signal, TradingMode, utcnow
from ..risk_management.manager import RiskDecision
from .broker import RC_POSITION_CLOSED, Broker, BrokerError

log = logging.getLogger(__name__)

OPEN_STATES = ("submitting", "unknown")


def make_client_id(signal: Signal) -> str:
    raw = f"{signal.strategy}|{signal.symbol}|{signal.timeframe}|{signal.time.isoformat()}|{signal.side.value}"
    return "q" + hashlib.sha1(raw.encode()).hexdigest()[:15]


class OrderManager:
    def __init__(self, broker: Broker, db, mode: TradingMode, magic: int, max_retries: int = 2,
                 retry_delay: float = 0.5, max_slippage_points: int = 20,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.broker = broker
        self.db = db
        self.mode = mode
        self.magic = magic
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.max_slippage_points = max_slippage_points
        self._sleep = sleep

    # ----------------------------------------------------------------- submit
    def submit(self, signal: Signal, decision: RiskDecision) -> tuple[str, Optional[OrderResult], str]:
        """Returns (status, result, client_id). status is one of
        filled | placed | rejected | unknown | duplicate."""
        cid = make_client_id(signal)
        reserved = self.db.reserve_order(
            client_id=cid, mode=self.mode.value, symbol=signal.symbol, side=signal.side.value,
            order_type=signal.order_type.value, volume=decision.volume, price=signal.entry_price,
            sl=signal.sl, tp=signal.tp, strategy=signal.strategy, risk_amount=decision.risk_amount,
            management=signal.management.to_dict(), signal_time=signal.time, timeframe=signal.timeframe)
        if not reserved:
            log.info("duplicate decision %s (%s %s) - not sending again", cid, signal.strategy, signal.symbol)
            return "duplicate", None, cid
        request = OrderRequest(
            client_id=cid, symbol=signal.symbol, side=signal.side, volume=decision.volume, sl=signal.sl,
            tp=signal.tp, order_type=signal.order_type, price=signal.entry_price,
            deviation_points=self.max_slippage_points, magic=self.magic, strategy=signal.strategy, comment=cid)
        self.db.audit("order_submit", {"client_id": cid, "symbol": signal.symbol, "side": signal.side.value,
                                       "volume": decision.volume, "sl": signal.sl, "tp": signal.tp,
                                       "strategy": signal.strategy, "risk_amount": round(decision.risk_amount, 2)})
        result: Optional[OrderResult] = None
        for attempt in range(self.max_retries + 1):
            try:
                result = self.broker.send_order(request)
            except BrokerError as exc:
                result = OrderResult(ok=False, message=f"broker error: {exc}", uncertain=True)
            if result.ok or not result.retryable:
                break
            log.warning("order %s not executed (%s) - retry %d/%d", cid, result.message, attempt + 1,
                        self.max_retries)
            self._sleep(self.retry_delay)
        assert result is not None
        return self._settle(cid, result, expected=decision.entry_price), result, cid

    def _settle(self, cid: str, result: OrderResult, expected: float = 0.0) -> str:
        if result.ok:
            status = "filled" if result.position_ticket or result.fill_price else "placed"
            self.db.update_order(cid, status=status, retcode=result.retcode, message=result.message,
                                 order_ticket=result.order_ticket, position_ticket=result.position_ticket,
                                 fill_price=result.fill_price)
            self.db.audit("order_result", {"client_id": cid, "status": status, "price": result.fill_price,
                                           "position": result.position_ticket, "expected_price": expected})
            return status
        if result.uncertain:
            # Do not resend. Look for evidence of execution right away.
            found = self._find_position(cid)
            if found:
                self.db.update_order(cid, status="filled", position_ticket=found.ticket,
                                     fill_price=found.price_open, message="recovered after uncertain result")
                self.db.audit("order_result", {"client_id": cid, "status": "filled", "recovered": True})
                return "filled"
            self.db.update_order(cid, status="unknown", retcode=result.retcode, message=result.message)
            self.db.audit("order_result", {"client_id": cid, "status": "unknown", "message": result.message})
            return "unknown"
        self.db.update_order(cid, status="rejected", retcode=result.retcode, message=result.message)
        self.db.audit("order_result", {"client_id": cid, "status": "rejected", "retcode": result.retcode,
                                       "message": result.message})
        return "rejected"

    def _find_position(self, cid: str) -> Optional[Position]:
        try:
            for p in self.broker.positions():
                if p.comment == cid:
                    return p
        except BrokerError:
            return None
        return None

    # -------------------------------------------------------------- reconcile
    def reconcile(self, now: datetime, grace: timedelta = timedelta(seconds=90)) -> list[dict]:
        """Settle orders left in an open state by a crash or a timeout, and
        promote placed pending orders that have since filled. Never resends.

        ``now`` is the broker clock (used to look up history); the age of an
        unresolved row is measured on the wall clock it was created with."""
        changes: list[dict] = []
        rows = self.db.orders_by_status(*OPEN_STATES, "placed")
        if not rows:
            return changes
        try:
            positions = {p.comment: p for p in self.broker.positions()}
            pending = {o.comment for o in self.broker.pending_orders()}
            oldest = min(datetime.fromisoformat(r["signal_time"] or r["created_at"]) for r in rows)
            closed = {t.client_id: t for t in self.broker.closed_trades(min(oldest, now) - timedelta(hours=1))}
        except BrokerError as exc:
            log.warning("reconcile postponed: %s", exc)
            return changes
        wall = utcnow()
        for r in rows:
            cid, age = r["client_id"], wall - datetime.fromisoformat(r["created_at"])
            if cid in positions:
                p = positions[cid]
                self.db.update_order(cid, status="filled", position_ticket=p.ticket, fill_price=p.price_open)
                changes.append({"client_id": cid, "status": "filled"})
            elif cid in closed:
                t = closed[cid]
                self.db.update_order(cid, status="filled", position_ticket=t.position_ticket,
                                     fill_price=t.price_open)
                changes.append({"client_id": cid, "status": "filled"})
            elif cid in pending:
                if r["status"] != "placed":
                    self.db.update_order(cid, status="placed")
                    changes.append({"client_id": cid, "status": "placed"})
            elif r["status"] == "placed":
                self.db.update_order(cid, status="cancelled", message="pending order expired or was removed")
                changes.append({"client_id": cid, "status": "cancelled"})
            elif age >= grace:
                self.db.update_order(cid, status="failed", message="no trace at broker after restart/timeout")
                changes.append({"client_id": cid, "status": "failed"})
        for c in changes:
            self.db.audit("order_reconciled", c)
        return changes

    # ------------------------------------------------------------ closed trades
    def sync_closed_trades(self, since: datetime) -> list[ClosedTrade]:
        """Record newly closed trades exactly once, enriched with what the
        database knows about the originating decision."""
        fresh: list[ClosedTrade] = []
        for t in self.broker.closed_trades(since):
            if t.magic and t.magic != self.magic:
                continue                      # not ours (manual trade or another EA)
            row = self.db.get_order(t.client_id) if t.client_id else None
            row = row or self.db.order_for_position(t.position_ticket)
            if row is None:
                continue                      # unknown origin: never attribute it to a strategy
            t.strategy = t.strategy or row["strategy"]
            t.client_id = row["client_id"]
            if not t.risk_amount:
                t.risk_amount = float(row["risk_amount"] or 0.0)
            t.sl = t.sl or float(row["sl"])
            t.tp = t.tp or float(row["tp"])
            if self.db.record_trade(t, self.mode.value):
                fresh.append(t)
                self.db.audit("trade_closed", {"client_id": t.client_id, "symbol": t.symbol,
                                               "profit": round(t.profit, 2), "r": round(t.r_multiple, 2),
                                               "exit": t.exit_reason})
        return fresh

    # ---------------------------------------------------------------- actions
    def modify_stop(self, position: Position, new_sl: float, reason: str) -> bool:
        res = self.broker.modify_position(position.ticket, new_sl, position.tp)
        if res.ok:
            self.db.audit("stop_moved", {"position": position.ticket, "symbol": position.symbol,
                                         "from": position.sl, "to": new_sl, "reason": reason})
        return res.ok

    def close(self, position: Position, reason: str) -> bool:
        res = self.broker.close_position(position.ticket, self.max_slippage_points, reason)
        if res.ok or res.retcode == RC_POSITION_CLOSED:
            self.db.audit("position_closed", {"position": position.ticket, "symbol": position.symbol,
                                              "reason": reason, "price": res.fill_price})
            return True
        log.error("failed to close position %s: %s", position.ticket, res.message)
        return False

    def flatten(self, reason: str) -> dict:
        """Emergency: cancel pending orders and close every position we own."""
        closed = failed = cancelled = 0
        for o in self.broker.pending_orders():
            if o.magic == self.magic and self.broker.cancel_order(o.ticket).ok:
                cancelled += 1
        for p in self.broker.positions():
            if p.magic != self.magic:
                continue
            if self.close(p, reason):
                closed += 1
            else:
                failed += 1
        out = {"closed": closed, "failed": failed, "cancelled": cancelled}
        self.db.audit("flatten", {"reason": reason, **out})
        return out

    def management_for(self, position: Position) -> Optional[dict]:
        row = self.db.order_for_position(position.ticket) or (self.db.get_order(position.comment)
                                                               if position.comment else None)
        if row is None:
            return None
        return {"management": json.loads(row["management"] or "{}"), "sl_initial": float(row["sl"]),
                "timeframe": row["timeframe"], "strategy": row["strategy"],
                "risk_amount": float(row["risk_amount"] or 0.0), "client_id": row["client_id"]}
