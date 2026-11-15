"""MetaTrader 5 adapter built on the official ``MetaTrader5`` Python package.

PLATFORM NOTE: MetaQuotes publishes that package for Windows only. It talks
to a locally running terminal over IPC, so this adapter must run on the same
Windows machine (desktop or VPS) as the terminal. On Linux/macOS use
``BridgeBroker`` against the gateway (``python main.py bridge``) running on
that Windows machine.

The module is injected (``mt5_module``) so the adapter logic can be unit
tested against a fake terminal on any OS.
"""
from __future__ import annotations

import logging
import sys
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from ..core.config import Secrets
from ..core.logging_setup import mask_login
from ..core.models import (UTC, AccountInfo, ClosedTrade, OrderRequest, OrderResult, OrderType, PendingOrder,
                           Position, Side, SymbolSpec, Tick, TradingMode, utcnow)
from .broker import (RC_CONNECTION, RC_INVALID, RC_LIVE_LOCKED, RC_POSITION_CLOSED, Broker, BrokerError,
                     result_from_retcode)
from .live_gate import LiveGate, LiveTradingLocked

log = logging.getLogger(__name__)

_TF_ATTR = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
            "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1"}
_DEAL_REASON = {0: "manual", 1: "manual", 2: "manual", 3: "strategy", 4: "sl", 5: "tp", 6: "stop_out"}
_ASSET_HINTS = (("crypto", "crypto"), ("metal", "metal"), ("indic", "index"), ("index", "index"),
                ("energ", "commodity"), ("commod", "commodity"), ("forex", "forex"), ("fx", "forex"))


def load_mt5_module():
    """Import the official package or explain precisely why it cannot work."""
    if sys.platform != "win32":
        raise BrokerError(
            "The official MetaTrader5 Python package only exists for Windows and needs a locally "
            f"running terminal; this process runs on '{sys.platform}'. Run the gateway "
            "(python main.py bridge) on the Windows machine/VPS that hosts the terminal and set "
            "broker_backend: bridge here.")
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - only on Windows without the package
        raise BrokerError("MetaTrader5 package is not installed: pip install MetaTrader5") from exc
    return mt5


class MT5Broker(Broker):
    name = "mt5"

    def __init__(self, mode: TradingMode, gate: Optional[LiveGate], magic: int, mt5_module: Any = None,
                 read_only: bool = False, server_utc_offset_hours: Optional[float] = None,
                 login: Optional[int] = None, password: Optional[str] = None, server: Optional[str] = None,
                 terminal_path: Optional[str] = None) -> None:
        self.mt5 = mt5_module if mt5_module is not None else load_mt5_module()
        self.mode = mode
        self.gate = gate
        self.magic = magic
        self.read_only = read_only or mode in (TradingMode.PAPER, TradingMode.BACKTEST)
        self._login, self._password, self._server, self._path = login, password, server, terminal_path
        self._offset_explicit = server_utc_offset_hours is not None
        self._offset = timedelta(hours=server_utc_offset_hours or 0.0)
        self._offset_checked = 0.0
        self._specs: dict[str, SymbolSpec] = {}
        self._initialized = False

    # ------------------------------------------------------------ connection
    def connect(self) -> bool:
        kwargs: dict[str, Any] = {}
        login = self._login or Secrets.get("MT5_LOGIN")
        password = self._password or Secrets.get("MT5_PASSWORD")
        server = self._server or Secrets.get("MT5_SERVER")
        path = self._path or Secrets.get("MT5_TERMINAL_PATH")
        if login:
            kwargs["login"] = int(login)
        if password:
            kwargs["password"] = password
        if server:
            kwargs["server"] = server
        args = (path,) if path else ()
        if not self.mt5.initialize(*args, **kwargs):
            code = self.mt5.last_error()
            self._initialized = False
            log.error("MT5 initialize failed: %s", code)
            return False
        self._initialized = True
        acct = self.account_info()
        log.info("MT5 connected: account %s on %s (%s)", mask_login(acct.login), acct.server,
                 "DEMO" if acct.is_demo else "REAL")
        if not self.read_only:
            if self.gate is None:
                self.disconnect()
                raise LiveTradingLocked("no live gate configured for a trading connection")
            try:
                self.gate.assert_can_trade(self.mode, acct)
            except LiveTradingLocked:
                self.disconnect()
                raise
        return True

    def disconnect(self) -> None:
        try:
            self.mt5.shutdown()
        finally:
            self._initialized = False

    def is_connected(self) -> bool:
        if not self._initialized:
            return False
        try:
            info = self.mt5.terminal_info()
            return bool(info and info.connected)
        except Exception:
            return False

    def _require(self) -> None:
        if not self._initialized:
            raise BrokerError("MT5 terminal is not initialized")

    def _err(self, what: str) -> BrokerError:
        return BrokerError(f"{what} failed: {self.mt5.last_error()}")

    # ------------------------------------------------------------------ time
    def _detect_offset(self) -> timedelta:
        """Broker bar/tick timestamps are SERVER time expressed as if it were
        UTC. Infer the offset from the freshest tick (rounded to 15 minutes)
        and re-check hourly so broker DST switches are followed. While quotes
        are stale (weekend) the last known offset is kept."""
        if self._offset_explicit:
            return self._offset
        if self._offset_checked and _time.time() - self._offset_checked < 3600:
            return self._offset
        latest: Optional[float] = None
        for symbol in list(self._specs):
            t = self.mt5.symbol_info_tick(symbol)
            if t is None:
                continue
            epoch = float(getattr(t, "time_msc", 0)) / 1000.0 or float(t.time)
            latest = epoch if latest is None else max(latest, epoch)
        if latest is None:
            return self._offset
        diff = latest - _time.time()
        rounded = round(diff / 900.0) * 900
        if abs(diff - rounded) < 120 and abs(rounded) <= 14 * 3600:
            if timedelta(seconds=rounded) != self._offset or not self._offset_checked:
                log.info("MT5 server time offset: %+.2f h", rounded / 3600)
            self._offset = timedelta(seconds=rounded)
            self._offset_checked = _time.time()
        return self._offset

    def _utc(self, epoch: float) -> datetime:
        return datetime.fromtimestamp(float(epoch), UTC) - self._detect_offset()

    def _server_epoch(self, t: datetime) -> datetime:
        return (t if t.tzinfo else t.replace(tzinfo=UTC)) + self._detect_offset()

    # ----------------------------------------------------------- market data
    def account_info(self) -> AccountInfo:
        self._require()
        a = self.mt5.account_info()
        if a is None:
            raise self._err("account_info")
        return AccountInfo(login=str(a.login), server=str(a.server), currency=str(a.currency),
                           balance=float(a.balance), equity=float(a.equity), margin=float(a.margin),
                           margin_free=float(a.margin_free), margin_level=float(a.margin_level or 0.0),
                           leverage=int(a.leverage),
                           is_demo=int(a.trade_mode) == int(self.mt5.ACCOUNT_TRADE_MODE_DEMO),
                           trade_allowed=bool(a.trade_allowed) and bool(getattr(a, "trade_expert", True)),
                           profit=float(a.profit))

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._require()
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(f"symbol '{symbol}' does not exist on this broker "
                              "(check the exact name incl. suffix in Market Watch)")
        if not info.visible and not self.mt5.symbol_select(symbol, True):
            raise self._err(f"symbol_select({symbol})")
        path = str(getattr(info, "path", "")).lower()
        asset = next((cls for hint, cls in _ASSET_HINTS if hint in path), "forex")
        spec = SymbolSpec(
            name=symbol, digits=int(info.digits), point=float(info.point),
            tick_size=float(info.trade_tick_size or info.point), tick_value=float(info.trade_tick_value),
            contract_size=float(info.trade_contract_size), volume_min=float(info.volume_min),
            volume_max=float(info.volume_max), volume_step=float(info.volume_step),
            stops_level=int(info.trade_stops_level), currency_base=str(info.currency_base),
            currency_profit=str(info.currency_profit), currency_margin=str(info.currency_margin),
            asset_class=asset,
            trade_allowed=int(info.trade_mode) == int(self.mt5.SYMBOL_TRADE_MODE_FULL))
        self._specs[symbol] = spec
        return spec

    def tick(self, symbol: str) -> Tick:
        self._require()
        if symbol not in self._specs:
            self.symbol_spec(symbol)
        t = self.mt5.symbol_info_tick(symbol)
        if t is None or not t.bid or not t.ask:
            raise BrokerError(f"no tick for {symbol}: {self.mt5.last_error()}")
        epoch = float(getattr(t, "time_msc", 0)) / 1000.0 or float(t.time)
        return Tick(symbol, self._utc(epoch), float(t.bid), float(t.ask))

    def _frame(self, rates) -> pd.DataFrame:
        df = pd.DataFrame(rates)
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "spread"],
                                index=pd.DatetimeIndex([], tz="UTC"))
        off = self._detect_offset()
        df.index = pd.to_datetime(df["time"], unit="s", utc=True) - off
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["open", "high", "low", "close", "volume", "spread"]].astype(float)

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self._require()
        if symbol not in self._specs:
            self.symbol_spec(symbol)
        data = self.mt5.copy_rates_from_pos(symbol, getattr(self.mt5, _TF_ATTR[timeframe]), 0, int(count))
        if data is None:
            raise self._err(f"copy_rates_from_pos({symbol},{timeframe})")
        return self._frame(data)

    def rates_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        self._require()
        if symbol not in self._specs:
            self.symbol_spec(symbol)
        data = self.mt5.copy_rates_range(symbol, getattr(self.mt5, _TF_ATTR[timeframe]),
                                         self._server_epoch(start), self._server_epoch(end))
        if data is None:
            raise self._err(f"copy_rates_range({symbol},{timeframe})")
        return self._frame(data)

    # -------------------------------------------------------------- positions
    def positions(self, symbol: Optional[str] = None) -> list[Position]:
        self._require()
        raw = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if raw is None:
            raise self._err("positions_get")
        return [Position(ticket=int(p.ticket), symbol=str(p.symbol),
                         side=Side.BUY if int(p.type) == int(self.mt5.POSITION_TYPE_BUY) else Side.SELL,
                         volume=float(p.volume), price_open=float(p.price_open), sl=float(p.sl), tp=float(p.tp),
                         time_open=self._utc(p.time), price_current=float(p.price_current),
                         profit=float(p.profit), swap=float(p.swap), magic=int(p.magic), comment=str(p.comment))
                for p in raw]

    def pending_orders(self, symbol: Optional[str] = None) -> list[PendingOrder]:
        self._require()
        raw = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        if raw is None:
            raise self._err("orders_get")
        m = self.mt5
        kinds = {int(m.ORDER_TYPE_BUY_LIMIT): (Side.BUY, OrderType.LIMIT),
                 int(m.ORDER_TYPE_SELL_LIMIT): (Side.SELL, OrderType.LIMIT),
                 int(m.ORDER_TYPE_BUY_STOP): (Side.BUY, OrderType.STOP),
                 int(m.ORDER_TYPE_SELL_STOP): (Side.SELL, OrderType.STOP)}
        out = []
        for o in raw:
            if int(o.type) not in kinds:
                continue
            side, kind = kinds[int(o.type)]
            exp = getattr(o, "time_expiration", 0)
            out.append(PendingOrder(ticket=int(o.ticket), symbol=str(o.symbol), side=side, order_type=kind,
                                    volume=float(o.volume_current), price=float(o.price_open), sl=float(o.sl),
                                    tp=float(o.tp), time_setup=self._utc(o.time_setup), magic=int(o.magic),
                                    comment=str(o.comment), expiry=self._utc(exp) if exp else None))
        return out

    # ---------------------------------------------------------------- trading
    def _guard(self) -> Optional[OrderResult]:
        """Per-order defence in depth: re-validate the right to trade."""
        if self.read_only:
            return result_from_retcode(RC_LIVE_LOCKED, "adapter is read-only (paper mode data source)")
        if not self._initialized:
            return result_from_retcode(RC_CONNECTION)
        try:
            self.gate.assert_can_trade(self.mode, self.account_info())  # type: ignore[union-attr]
        except LiveTradingLocked as exc:
            log.error("order blocked by live gate: %s", exc)
            return result_from_retcode(RC_LIVE_LOCKED, str(exc))
        except BrokerError:
            return result_from_retcode(RC_CONNECTION)
        return None

    def _filling(self, symbol: str) -> int:
        info = self.mt5.symbol_info(symbol)
        mode = int(getattr(info, "filling_mode", 0)) if info else 0
        if mode & 1:
            return int(self.mt5.ORDER_FILLING_FOK)
        if mode & 2:
            return int(self.mt5.ORDER_FILLING_IOC)
        return int(self.mt5.ORDER_FILLING_RETURN)

    def _send(self, req: dict) -> OrderResult:
        res = self.mt5.order_send(req)
        if res is None:
            code = self.mt5.last_error()
            log.error("order_send returned None: %s", code)
            # No answer from the terminal: execution state is unknown.
            return OrderResult(ok=False, retcode=RC_CONNECTION, message=f"no response: {code}", uncertain=True)
        out = result_from_retcode(int(res.retcode), str(getattr(res, "comment", "")),
                                  order_ticket=int(getattr(res, "order", 0) or 0),
                                  fill_price=float(getattr(res, "price", 0.0) or 0.0),
                                  volume=float(getattr(res, "volume", 0.0) or 0.0), time=utcnow())
        return out

    def send_order(self, request: OrderRequest) -> OrderResult:
        blocked = self._guard()
        if blocked:
            return blocked
        m = self.mt5
        spec = self._specs.get(request.symbol) or self.symbol_spec(request.symbol)
        req: dict[str, Any] = {
            "symbol": request.symbol, "volume": float(request.volume),
            "sl": round(request.sl, spec.digits), "tp": round(request.tp, spec.digits),
            "deviation": int(request.deviation_points), "magic": int(request.magic or self.magic),
            "comment": (request.comment or request.client_id)[:31],
        }
        if request.order_type is OrderType.MARKET:
            t = self.mt5.symbol_info_tick(request.symbol)
            if t is None:
                return result_from_retcode(RC_INVALID, "no tick")
            req.update(action=m.TRADE_ACTION_DEAL,
                       type=m.ORDER_TYPE_BUY if request.side is Side.BUY else m.ORDER_TYPE_SELL,
                       price=float(t.ask if request.side is Side.BUY else t.bid),
                       type_filling=self._filling(request.symbol), type_time=m.ORDER_TIME_GTC)
        else:
            if request.price is None:
                return result_from_retcode(RC_INVALID, "pending order without price")
            table = {(Side.BUY, OrderType.LIMIT): m.ORDER_TYPE_BUY_LIMIT,
                     (Side.SELL, OrderType.LIMIT): m.ORDER_TYPE_SELL_LIMIT,
                     (Side.BUY, OrderType.STOP): m.ORDER_TYPE_BUY_STOP,
                     (Side.SELL, OrderType.STOP): m.ORDER_TYPE_SELL_STOP}
            req.update(action=m.TRADE_ACTION_PENDING, type=table[(request.side, request.order_type)],
                       price=round(request.price, spec.digits), type_filling=m.ORDER_FILLING_RETURN,
                       type_time=m.ORDER_TIME_GTC)
            if request.expiry is not None:
                req.update(type_time=m.ORDER_TIME_SPECIFIED,
                           expiration=int(self._server_epoch(request.expiry).timestamp()))
        result = self._send(req)
        if result.ok and request.order_type is OrderType.MARKET:
            # Order verification: locate the resulting position.
            pos = self._find_position(request.symbol, req["comment"], result.order_ticket)
            if pos:
                result.position_ticket = pos.ticket
                result.fill_price = pos.price_open
                result.volume = pos.volume
        return result

    def _find_position(self, symbol: str, comment: str, order_ticket: int) -> Optional[Position]:
        try:
            for p in self.positions(symbol):
                if p.ticket == order_ticket or (comment and p.comment == comment):
                    return p
        except BrokerError:
            return None
        return None

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        blocked = self._guard()
        if blocked:
            return blocked
        pos = self.mt5.positions_get(ticket=int(ticket))
        if not pos:
            return result_from_retcode(RC_POSITION_CLOSED)
        p = pos[0]
        spec = self._specs.get(p.symbol) or self.symbol_spec(p.symbol)
        return self._send({"action": self.mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": p.symbol,
                           "sl": round(sl, spec.digits), "tp": round(tp, spec.digits),
                           "magic": int(p.magic)})

    def close_position(self, ticket: int, deviation_points: int = 20, reason: str = "") -> OrderResult:
        # Closing reduces risk, so it is allowed whenever the adapter may trade at all.
        if self.read_only:
            return result_from_retcode(RC_LIVE_LOCKED, "adapter is read-only")
        if not self._initialized:
            return result_from_retcode(RC_CONNECTION)
        pos = self.mt5.positions_get(ticket=int(ticket))
        if not pos:
            return result_from_retcode(RC_POSITION_CLOSED)
        p = pos[0]
        t = self.mt5.symbol_info_tick(p.symbol)
        if t is None:
            return result_from_retcode(RC_INVALID, "no tick")
        m = self.mt5
        is_buy = int(p.type) == int(m.POSITION_TYPE_BUY)
        res = self._send({
            "action": m.TRADE_ACTION_DEAL, "position": int(ticket), "symbol": p.symbol,
            "volume": float(p.volume), "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY,
            "price": float(t.bid if is_buy else t.ask), "deviation": int(deviation_points),
            "magic": int(p.magic), "comment": (reason or "close")[:31],
            "type_filling": self._filling(p.symbol), "type_time": m.ORDER_TIME_GTC})
        if res.ok:
            res.position_ticket = int(ticket)
        return res

    def cancel_order(self, ticket: int) -> OrderResult:
        if self.read_only:
            return result_from_retcode(RC_LIVE_LOCKED, "adapter is read-only")
        return self._send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})

    # ---------------------------------------------------------------- history
    def closed_trades(self, since: datetime) -> list[ClosedTrade]:
        self._require()
        m = self.mt5
        start = self._server_epoch(since) - timedelta(days=30)   # entry deal may be much older
        end = self._server_epoch(utcnow()) + timedelta(days=1)
        deals = m.history_deals_get(start, end)
        if deals is None:
            raise self._err("history_deals_get")
        by_pos: dict[int, list] = defaultdict(list)
        for d in deals:
            if int(d.type) in (int(m.DEAL_TYPE_BUY), int(m.DEAL_TYPE_SELL)):
                by_pos[int(d.position_id)].append(d)
        open_tickets = {p.ticket for p in self.positions()}
        out: list[ClosedTrade] = []
        for pid, ds in by_pos.items():
            if pid in open_tickets:
                continue
            ins = [d for d in ds if int(d.entry) == int(m.DEAL_ENTRY_IN)]
            outs = [d for d in ds if int(d.entry) != int(m.DEAL_ENTRY_IN)]
            if not ins or not outs:
                continue
            first, last = min(ins, key=lambda d: d.time), max(outs, key=lambda d: d.time)
            close_time = self._utc(getattr(last, "time_msc", last.time * 1000) / 1000.0)
            if close_time < since:
                continue
            vol_in = sum(float(d.volume) for d in ins)
            vol_out = sum(float(d.volume) for d in outs)
            if vol_out + 1e-9 < vol_in:
                continue                        # partially closed: still open
            commission = sum(float(d.commission) + float(getattr(d, "fee", 0.0)) for d in ds)
            swap = sum(float(d.swap) for d in ds)
            gross = sum(float(d.profit) for d in ds)
            out.append(ClosedTrade(
                position_ticket=pid, symbol=str(first.symbol),
                side=Side.BUY if int(first.type) == int(m.DEAL_TYPE_BUY) else Side.SELL, volume=vol_in,
                price_open=sum(float(d.price) * float(d.volume) for d in ins) / vol_in,
                price_close=sum(float(d.price) * float(d.volume) for d in outs) / vol_out,
                time_open=self._utc(first.time), time_close=close_time, profit=gross + commission + swap,
                commission=commission, swap=swap, exit_reason=_DEAL_REASON.get(int(last.reason), "other"),
                magic=int(first.magic), comment=str(first.comment), client_id=str(first.comment)))
        return sorted(out, key=lambda t: t.time_close)

