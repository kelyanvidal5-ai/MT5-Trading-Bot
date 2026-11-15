"""Simulated broker used for backtests and paper trading.

It reproduces the parts of MetaTrader's behaviour that matter for honest
results: bid/ask accounting, spread, commission, swap, adverse slippage,
stop-level validation, margin, gap-through fills on stops, pending orders,
and MT5-style return codes for rejected requests.

Bars are BID bars (MetaTrader convention). Longs open at ask and close at
bid; shorts open at bid and close at ask (= bid + spread).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from ..backtesting.costs import CostModel
from ..core.models import (UTC, AccountInfo, ClosedTrade, OrderRequest, OrderResult, OrderType, PendingOrder,
                           Position, Side, SymbolSpec, Tick, utcnow)
from ..market_data.specs import generic_spec
from ..market_data.timeframes import drop_incomplete
from .broker import (RC_DONE, RC_INVALID, RC_INVALID_PRICE, RC_INVALID_STOPS, RC_INVALID_VOLUME, RC_MARKET_CLOSED,
                     RC_NO_CHANGES, RC_NO_MONEY, RC_PLACED, RC_POSITION_CLOSED, RC_PRICE_OFF, RC_REQUOTE,
                     RC_TRADE_DISABLED, Broker, BrokerError, MarketDataSource, result_from_retcode)

_ROLL_SECONDS = 22 * 3600


@dataclass
class _Pos:
    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    time_open: str
    sl_initial: float
    tp_initial: float
    risk_amount: float
    commission_in: float
    swap: float
    strategy: str
    client_id: str
    magic: int
    comment: str
    roll_idx: int

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    @property
    def opened(self) -> datetime:
        return datetime.fromisoformat(self.time_open)


@dataclass
class _Pend:
    ticket: int
    symbol: str
    side: str
    order_type: str
    volume: float
    price: float
    sl: float
    tp: float
    time_setup: str
    expiry: Optional[str]
    strategy: str
    client_id: str
    magic: int
    comment: str


def _roll_index(t: datetime) -> int:
    return int(math.floor((t.timestamp() - _ROLL_SECONDS) / 86400.0))


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, data: Optional[MarketDataSource] = None, balance: float = 10_000.0,
                 currency: str = "USD", leverage: int = 30, cost: Optional[CostModel] = None,
                 specs: Optional[dict[str, SymbolSpec]] = None,
                 on_change: Optional[Callable[[dict], None]] = None, sync_timeframe: str = "M1") -> None:
        self.data = data
        self.currency = currency
        self.leverage = leverage
        self.cost = cost or CostModel()
        self._balance = float(balance)
        self._specs: dict[str, SymbolSpec] = dict(specs or {})
        self._generic: set[str] = set()
        self._px: dict[str, tuple[float, float, datetime]] = {}
        self._fill_ctx: dict[str, tuple[float, float]] = {}      # symbol -> (bar_range, bar_seconds)
        self._positions: dict[int, _Pos] = {}
        self._pending: dict[int, _Pend] = {}
        self._closed: list[ClosedTrade] = []
        self._next_ticket = 1_000_001
        self._on_change = on_change
        self._connected = True
        self._last_bar_seen: dict[str, datetime] = {}
        self.sync_timeframe = sync_timeframe
        self.trade_allowed = True

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        return {
            "balance": self._balance, "next_ticket": self._next_ticket,
            "positions": [asdict(p) for p in self._positions.values()],
            "pending": [asdict(p) for p in self._pending.values()],
        }

    def load_state(self, state: Optional[dict]) -> None:
        if not state:
            return
        self._balance = float(state["balance"])
        self._next_ticket = int(state["next_ticket"])
        self._positions = {p["ticket"]: _Pos(**p) for p in state.get("positions", [])}
        self._pending = {p["ticket"]: _Pend(**p) for p in state.get("pending", [])}

    def _changed(self) -> None:
        if self._on_change:
            self._on_change(self.to_state())

    # ---------------------------------------------------------- market access
    def is_connected(self) -> bool:
        return self._connected and (self.data.is_connected() if self.data else True)

    def connect(self) -> bool:
        self._connected = True
        return self.data.connect() if self.data else True

    def now(self) -> datetime:
        if self.data:
            return self.data.now()
        if self._px:
            return max(t for _, _, t in self._px.values())
        return utcnow()

    def register_spec(self, spec: SymbolSpec) -> None:
        self._specs[spec.name] = spec
        self._generic.discard(spec.name)

    def _base_spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            if self.data is not None:
                self._specs[symbol] = self.data.symbol_spec(symbol)
                if getattr(self.data, "generic_specs", False):
                    self._generic.add(symbol)
            else:
                self._specs[symbol] = generic_spec(symbol, self.currency)
                self._generic.add(symbol)
        return self._specs[symbol]

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        spec = self._base_spec(symbol)
        if symbol in self._generic:
            rate = self._quote_to_account(spec)
            if rate:
                spec = replace(spec, tick_value=spec.tick_size * spec.contract_size * rate)
        return spec

    def _mid(self, symbol: str) -> Optional[float]:
        px = self._px.get(symbol)
        return (px[0] + px[1]) / 2 if px else None

    def _quote_to_account(self, spec: SymbolSpec) -> Optional[float]:
        quote, acct = spec.currency_profit, self.currency
        if not quote or quote == acct:
            return 1.0
        if spec.currency_base == acct:
            mid = self._mid(spec.name)
            return 1.0 / mid if mid else None
        suffix = spec.name[6:]
        direct, inverse = self._mid(f"{quote}{acct}{suffix}"), self._mid(f"{acct}{quote}{suffix}")
        if direct:
            return direct
        if inverse:
            return 1.0 / inverse
        return None

    def tick(self, symbol: str) -> Tick:
        if self.data is not None:
            t = self.data.tick(symbol)
            self.set_price(symbol, t.bid, t.ask, t.time)
            return t
        if symbol not in self._px:
            raise BrokerError(f"no price for {symbol}")
        bid, ask, when = self._px[symbol]
        return Tick(symbol, when, bid, ask)

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if self.data is None:
            raise BrokerError("paper broker has no data source attached")
        return self.data.rates(symbol, timeframe, count)

    # ------------------------------------------------------------- accounting
    def _floating(self, p: _Pos) -> float:
        px = self._px.get(p.symbol)
        if not px:
            return 0.0
        close = px[0] if p.side == "buy" else px[1]
        return (close - p.price_open) * p.sign * p.volume * self.symbol_spec(p.symbol).value_per_price_unit()

    def _margin(self, symbol: str, volume: float, price: float) -> float:
        spec = self.symbol_spec(symbol)
        return volume * spec.value_per_price_unit() * price * spec.margin_rate / self.leverage

    def account_info(self) -> AccountInfo:
        floating = sum(self._floating(p) + p.swap for p in self._positions.values())
        margin = sum(self._margin(p.symbol, p.volume, p.price_open) for p in self._positions.values())
        equity = self._balance + floating
        return AccountInfo(login="PAPER", server="internal-simulator", currency=self.currency,
                           balance=round(self._balance, 2), equity=round(equity, 2), margin=round(margin, 2),
                           margin_free=round(equity - margin, 2),
                           margin_level=round(equity / margin * 100, 2) if margin > 0 else 0.0,
                           leverage=self.leverage, is_demo=True, trade_allowed=self.trade_allowed,
                           profit=round(floating, 2))

    def positions(self, symbol: Optional[str] = None) -> list[Position]:
        out = []
        for p in self._positions.values():
            if symbol and p.symbol != symbol:
                continue
            px = self._px.get(p.symbol)
            cur = (px[0] if p.side == "buy" else px[1]) if px else p.price_open
            out.append(Position(ticket=p.ticket, symbol=p.symbol, side=Side(p.side), volume=p.volume,
                                price_open=p.price_open, sl=p.sl, tp=p.tp, time_open=p.opened,
                                price_current=cur, profit=round(self._floating(p), 2), swap=round(p.swap, 2),
                                commission=-round(p.commission_in, 2), magic=p.magic, comment=p.comment))
        return out

    def pending_orders(self, symbol: Optional[str] = None) -> list[PendingOrder]:
        return [PendingOrder(ticket=o.ticket, symbol=o.symbol, side=Side(o.side), order_type=OrderType(o.order_type),
                             volume=o.volume, price=o.price, sl=o.sl, tp=o.tp,
                             time_setup=datetime.fromisoformat(o.time_setup), magic=o.magic, comment=o.comment,
                             expiry=datetime.fromisoformat(o.expiry) if o.expiry else None)
                for o in self._pending.values() if not symbol or o.symbol == symbol]

    def closed_trades(self, since: datetime) -> list[ClosedTrade]:
        return [t for t in self._closed if t.time_close >= since]

    @property
    def all_closed_trades(self) -> list[ClosedTrade]:
        return self._closed

    @property
    def balance(self) -> float:
        return self._balance

    # ------------------------------------------------------------ price feeds
    def set_fill_context(self, symbol: str, bar_range: float, bar_seconds: float) -> None:
        self._fill_ctx[symbol] = (bar_range, bar_seconds)

    def _slip(self, symbol: str) -> float:
        rng_, secs = self._fill_ctx.get(symbol, (0.0, 60.0))
        return self.cost.slippage_price(self._base_spec(symbol).point, rng_, secs)

    def set_price(self, symbol: str, bid: float, ask: float, time: datetime) -> None:
        """Tick update. Triggers stops/targets/pending orders at tick prices."""
        self._px[symbol] = (bid, ask, time)
        self._apply_swaps(symbol, time)
        changed = False
        for o in [o for o in self._pending.values() if o.symbol == symbol]:
            changed |= self._pending_on_tick(o, bid, ask, time)
        for p in [p for p in self._positions.values() if p.symbol == symbol]:
            if p.side == "buy":
                if p.sl > 0 and bid <= p.sl:
                    self._close(p, bid, time, "sl"); changed = True
                elif p.tp > 0 and bid >= p.tp:
                    self._close(p, p.tp, time, "tp"); changed = True
            else:
                if p.sl > 0 and ask >= p.sl:
                    self._close(p, ask, time, "sl"); changed = True
                elif p.tp > 0 and ask <= p.tp:
                    self._close(p, p.tp, time, "tp"); changed = True
        if changed:
            self._changed()

    def process_bar(self, symbol: str, time_open: datetime, o: float, h: float, l: float, c: float,
                    spread: float, time_close: datetime, only_opened_before: Optional[datetime] = None) -> None:
        """Run one completed BID bar through pending orders and open positions,
        then leave the price at the bar close. ``spread`` is in price units."""
        changed = False
        for od in [x for x in self._pending.values() if x.symbol == symbol]:
            if only_opened_before and datetime.fromisoformat(od.time_setup) > only_opened_before:
                continue
            changed |= self._pending_on_bar(od, o, h, l, spread, time_open, time_close)
        for p in [x for x in self._positions.values() if x.symbol == symbol]:
            if only_opened_before and p.opened > only_opened_before:
                continue
            slip = self._slip(symbol)
            if p.side == "buy":
                if p.sl > 0 and l <= p.sl:
                    self._close(p, min(p.sl, o) - slip, time_close, "sl"); changed = True
                elif p.tp > 0 and h >= p.tp:
                    self._close(p, p.tp, time_close, "tp"); changed = True
            else:
                if p.sl > 0 and h + spread >= p.sl:
                    self._close(p, max(p.sl, o + spread) + slip, time_close, "sl"); changed = True
                elif p.tp > 0 and l + spread <= p.tp:
                    self._close(p, p.tp, time_close, "tp"); changed = True
        self._px[symbol] = (c, c + spread, time_close)
        self._apply_swaps(symbol, time_close)
        if changed:
            self._changed()

    def sync(self) -> None:
        """Paper-live upkeep: refresh ticks for symbols with exposure and run
        freshly completed bars through the trigger logic so intrabar stop/target
        touches between polls are not missed."""
        if self.data is None:
            return
        symbols = {p.symbol for p in self._positions.values()} | {o.symbol for o in self._pending.values()}
        for symbol in symbols:
            tf = getattr(self.data, "min_timeframe", None) or self.sync_timeframe
            try:
                now = pd.Timestamp(self.data.now())
                bars = drop_incomplete(self.data.rates(symbol, tf, 6), tf, now)
            except Exception:
                bars = None
            if bars is not None and len(bars):
                last = self._last_bar_seen.get(symbol)
                spec = self._base_spec(symbol)
                step = bars.index[1] - bars.index[0] if len(bars) > 1 else pd.Timedelta(minutes=1)
                for ts, row in bars.iterrows():
                    if last is not None and ts <= last:
                        continue
                    spread = self.cost.spread_price(symbol, spec.point, row.get("spread"))
                    self.process_bar(symbol, ts.to_pydatetime(), row.open, row.high, row.low, row.close,
                                     spread, (ts + step).to_pydatetime(), only_opened_before=ts.to_pydatetime())
                self._last_bar_seen[symbol] = bars.index[-1]
            self.tick(symbol)

    # ------------------------------------------------------------------ swaps
    def _apply_swaps(self, symbol: str, time: datetime) -> None:
        idx = _roll_index(time)
        for p in self._positions.values():
            if p.symbol != symbol or idx <= p.roll_idx:
                continue
            spec = self.symbol_spec(symbol)
            for day in range(p.roll_idx + 1, idx + 1):
                roll_day = datetime.fromtimestamp(day * 86400 + _ROLL_SECONDS, UTC).weekday()
                if roll_day >= 5:
                    continue
                mult = 3 if roll_day == 2 else 1
                pts = self.cost.swap_long_points if p.side == "buy" else self.cost.swap_short_points
                p.swap += pts * spec.point * spec.value_per_price_unit() * p.volume * mult
            p.roll_idx = idx

    # ----------------------------------------------------------------- orders
    def _validate_volume(self, spec: SymbolSpec, volume: float) -> bool:
        if volume < spec.volume_min - 1e-9 or volume > spec.volume_max + 1e-9:
            return False
        steps = (volume - spec.volume_min) / spec.volume_step
        return abs(steps - round(steps)) < 1e-6

    @staticmethod
    def _stops_ok(side: str, ref_bid: float, ref_ask: float, sl: float, tp: float, min_dist: float) -> bool:
        if side == "buy":
            if sl > 0 and sl > ref_bid - min_dist:
                return False
            if tp > 0 and tp < ref_ask + min_dist:
                return False
        else:
            if sl > 0 and sl < ref_ask + min_dist:
                return False
            if tp > 0 and tp > ref_bid - min_dist:
                return False
        return True

    def send_order(self, request: OrderRequest) -> OrderResult:
        if not self.is_connected():
            raise BrokerError("paper broker disconnected")
        if not self.trade_allowed:
            return result_from_retcode(RC_TRADE_DISABLED)
        try:
            spec = self.symbol_spec(request.symbol)
        except ValueError as exc:
            return result_from_retcode(RC_INVALID, str(exc))
        if request.symbol not in self._px:
            if self.data is not None:
                self.tick(request.symbol)
            else:
                return result_from_retcode(RC_MARKET_CLOSED, "no quotes")
        bid, ask, now = self._px[request.symbol]
        if not self._validate_volume(spec, request.volume):
            return result_from_retcode(RC_INVALID_VOLUME)
        min_dist = spec.stops_level * spec.point
        side = request.side.value

        if request.order_type is OrderType.MARKET:
            if not self._stops_ok(side, bid, ask, request.sl, request.tp, min_dist):
                return result_from_retcode(RC_INVALID_STOPS)
            slip = self._slip(request.symbol)
            if slip > request.deviation_points * spec.point:
                return result_from_retcode(RC_REQUOTE, "slippage beyond allowed deviation")
            price = ask + slip if side == "buy" else bid - slip
            return self._open(request, price, now)

        if request.price is None or request.price <= 0:
            return result_from_retcode(RC_INVALID_PRICE)
        px = request.price
        if request.order_type is OrderType.LIMIT:
            valid = px < ask - min_dist if side == "buy" else px > bid + min_dist
        else:
            valid = px > ask + min_dist if side == "buy" else px < bid - min_dist
        if not valid:
            return result_from_retcode(RC_INVALID_PRICE)
        if not self._stops_ok(side, px, px, request.sl, request.tp, min_dist):
            return result_from_retcode(RC_INVALID_STOPS)
        ticket = self._ticket()
        self._pending[ticket] = _Pend(ticket, request.symbol, side, request.order_type.value, request.volume,
                                      px, request.sl, request.tp, now.isoformat(),
                                      request.expiry.isoformat() if request.expiry else None,
                                      request.strategy, request.client_id, request.magic, request.comment)
        self._changed()
        return result_from_retcode(RC_PLACED, order_ticket=ticket, volume=request.volume, time=now)

    def _ticket(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def _open(self, req, price: float, now: datetime, ticket: Optional[int] = None) -> OrderResult:
        spec = self.symbol_spec(req.symbol)
        side = req.side if isinstance(req.side, str) else req.side.value
        price = round(price, spec.digits)
        acct = self.account_info()
        commission = self.cost.commission(req.volume)
        if self._margin(req.symbol, req.volume, price) + commission > acct.margin_free:
            return result_from_retcode(RC_NO_MONEY)
        ticket = ticket or self._ticket()
        risk = abs(price - req.sl) * req.volume * spec.value_per_price_unit() if req.sl > 0 else 0.0
        self._positions[ticket] = _Pos(ticket, req.symbol, side, req.volume, price, req.sl, req.tp,
                                       now.isoformat(), req.sl, req.tp, risk, commission, 0.0, req.strategy,
                                       req.client_id, req.magic, req.comment, _roll_index(now))
        self._balance -= commission
        self._changed()
        return result_from_retcode(RC_DONE, order_ticket=ticket, position_ticket=ticket, fill_price=price,
                                   volume=req.volume, time=now)

    def _close(self, p: _Pos, price: float, now: datetime, reason: str) -> ClosedTrade:
        spec = self.symbol_spec(p.symbol)
        price = round(price, spec.digits)
        gross = (price - p.price_open) * p.sign * p.volume * spec.value_per_price_unit()
        commission_out = self.cost.commission(p.volume)
        self._balance += gross - commission_out + p.swap
        del self._positions[p.ticket]
        trade = ClosedTrade(
            position_ticket=p.ticket, symbol=p.symbol, side=Side(p.side), volume=p.volume,
            price_open=p.price_open, price_close=price, time_open=p.opened, time_close=now,
            profit=gross - commission_out - p.commission_in + p.swap, commission=-(commission_out + p.commission_in),
            swap=p.swap, sl=p.sl_initial, tp=p.tp_initial, risk_amount=p.risk_amount, strategy=p.strategy,
            exit_reason=reason, client_id=p.client_id, magic=p.magic, comment=p.comment)
        self._closed.append(trade)
        return trade

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return result_from_retcode(RC_POSITION_CLOSED)
        spec = self._base_spec(p.symbol)
        sl, tp = round(sl, spec.digits), round(tp, spec.digits)
        if abs(sl - p.sl) < spec.point / 2 and abs(tp - p.tp) < spec.point / 2:
            return result_from_retcode(RC_NO_CHANGES)
        bid, ask, now = self._px[p.symbol]
        if not self._stops_ok(p.side, bid, ask, sl, tp, spec.stops_level * spec.point):
            return result_from_retcode(RC_INVALID_STOPS)
        p.sl, p.tp = sl, tp
        self._changed()
        return result_from_retcode(RC_DONE, position_ticket=ticket, time=now)

    def close_position(self, ticket: int, deviation_points: int = 20, reason: str = "manual") -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return result_from_retcode(RC_POSITION_CLOSED)
        if p.symbol not in self._px:
            return result_from_retcode(RC_PRICE_OFF)
        if self.data is not None:
            self.tick(p.symbol)
            if ticket not in self._positions:
                return result_from_retcode(RC_POSITION_CLOSED)
        bid, ask, now = self._px[p.symbol]
        slip = self._slip(p.symbol)
        price = bid - slip if p.side == "buy" else ask + slip
        trade = self._close(p, price, now, reason or "manual")
        self._changed()
        return result_from_retcode(RC_DONE, position_ticket=ticket, fill_price=trade.price_close,
                                   volume=trade.volume, time=now)

    def cancel_order(self, ticket: int) -> OrderResult:
        if ticket not in self._pending:
            return result_from_retcode(RC_INVALID, "unknown order")
        del self._pending[ticket]
        self._changed()
        return result_from_retcode(RC_DONE, order_ticket=ticket)

    # --------------------------------------------------------- pending orders
    def _expired(self, o: _Pend, now: datetime) -> bool:
        if o.expiry and now >= datetime.fromisoformat(o.expiry):
            del self._pending[o.ticket]
            return True
        return False

    def _fill_pending(self, o: _Pend, price: float, now: datetime) -> bool:
        del self._pending[o.ticket]
        req = OrderRequest(client_id=o.client_id, symbol=o.symbol, side=Side(o.side), volume=o.volume, sl=o.sl,
                           tp=o.tp, magic=o.magic, strategy=o.strategy, comment=o.comment)
        self._open(req, price, now, ticket=o.ticket)
        return True

    def _pending_on_tick(self, o: _Pend, bid: float, ask: float, now: datetime) -> bool:
        if self._expired(o, now):
            return True
        if o.order_type == "limit":
            if o.side == "buy" and ask <= o.price:
                return self._fill_pending(o, o.price, now)
            if o.side == "sell" and bid >= o.price:
                return self._fill_pending(o, o.price, now)
        else:
            if o.side == "buy" and ask >= o.price:
                return self._fill_pending(o, ask + self._slip(o.symbol), now)
            if o.side == "sell" and bid <= o.price:
                return self._fill_pending(o, bid - self._slip(o.symbol), now)
        return False

    def _pending_on_bar(self, o: _Pend, op: float, h: float, l: float, spread: float, t_open: datetime,
                        t_close: datetime) -> bool:
        if self._expired(o, t_open):
            return True
        slip = self._slip(o.symbol)
        fill: Optional[float] = None
        if o.order_type == "limit":
            if o.side == "buy" and l + spread <= o.price:
                fill = min(o.price, op + spread)
            elif o.side == "sell" and h >= o.price:
                fill = max(o.price, op)
        else:
            if o.side == "buy" and h + spread >= o.price:
                fill = max(o.price, op + spread) + slip
            elif o.side == "sell" and l <= o.price:
                fill = min(o.price, op) - slip
        if fill is None:
            return False
        self._fill_pending(o, fill, t_close)
        # Conservative: the stop may be hit on the fill bar; the target may not.
        p = self._positions.get(o.ticket)
        if p is not None and p.sl > 0:
            if p.side == "buy" and l <= p.sl:
                self._close(p, p.sl - slip, t_close, "sl")
            elif p.side == "sell" and h + spread >= p.sl:
                self._close(p, p.sl + slip, t_close, "sl")
        return True

