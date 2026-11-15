"""Domain models shared by every layer of the platform.

These are plain dataclasses with no behaviour that depends on a broker, so the
same objects flow through the backtester, the paper broker and the MT5 adapter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TradingMode(str, Enum):
    """Where orders go.

    BACKTEST  historical simulation
    PAPER     live loop, orders filled by the internal simulator (default)
    DEMO      orders sent to an MT5 *demo* account (verified at connect time)
    LIVE      real money - locked behind the live-trading gate
    """

    BACKTEST = "backtest"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"

    @property
    def minutes(self) -> int:
        return _TF_MINUTES[self.value]

    @property
    def delta(self) -> timedelta:
        return timedelta(minutes=self.minutes)

    @property
    def pandas_rule(self) -> str:
        return _TF_PANDAS[self.value]


_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}
_TF_PANDAS = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h",
              "D1": "1D", "W1": "7D"}


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"   # disorderly, abnormally high volatility
    QUIET = "quiet"         # compressed volatility, no direction
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SymbolSpec:
    """Contract specification. Values come from the broker, never from guesses,
    except for the clearly-labelled generic specs used by the simulator."""

    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float            # account-currency value of one tick_size move for 1.0 lot
    contract_size: float
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level: int = 0         # minimum SL/TP distance in points
    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""
    asset_class: str = "forex"   # forex | metal | index | crypto | commodity
    margin_rate: float = 1.0
    trade_allowed: bool = True

    @property
    def pip(self) -> float:
        """Conventional pip (10 points for 3/5-digit quotes)."""
        return self.point * 10 if self.digits in (3, 5) else self.point

    def value_per_price_unit(self) -> float:
        """Account-currency P/L of a 1.0 price move for 1.0 lot."""
        return self.tick_value / self.tick_size if self.tick_size else 0.0


@dataclass
class Tick:
    symbol: str
    time: datetime
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0


@dataclass
class AccountInfo:
    login: str
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float          # percent; 0 when no margin is used
    leverage: int
    is_demo: bool
    trade_allowed: bool
    profit: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ManagementPlan:
    """How an open position is managed after entry. All moves are one-way:
    the stop may only be tightened, never widened."""

    breakeven_at_r: Optional[float] = None      # move SL to entry once price moved this many R
    trail_atr_mult: Optional[float] = None      # ATR trailing stop multiple
    trail_start_r: float = 1.0                  # trailing starts after this many R
    max_bars: Optional[int] = None              # time stop, in entry-timeframe bars
    atr: float = 0.0                            # ATR at signal time (price units)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ManagementPlan":
        if not d:
            return cls()
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Signal:
    """A trade proposal produced by a strategy at the close of a bar."""

    strategy: str
    symbol: str
    timeframe: str
    time: datetime               # close time of the bar that produced the signal
    side: Side
    sl: float
    tp: float
    ref_price: float             # close of the signal bar
    strength: float = 0.5        # strategy's own 0..1 quality estimate
    order_type: OrderType = OrderType.MARKET
    entry_price: Optional[float] = None   # for pending orders
    reasons: list[str] = field(default_factory=list)
    management: ManagementPlan = field(default_factory=ManagementPlan)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_distance(self) -> float:
        ref = self.entry_price if self.entry_price is not None else self.ref_price
        return abs(ref - self.sl)

    @property
    def reward_risk(self) -> float:
        ref = self.entry_price if self.entry_price is not None else self.ref_price
        risk = abs(ref - self.sl)
        return abs(self.tp - ref) / risk if risk > 0 else 0.0


@dataclass
class OrderRequest:
    client_id: str               # idempotency key, unique per trading decision
    symbol: str
    side: Side
    volume: float
    sl: float
    tp: float
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None
    deviation_points: int = 20
    magic: int = 0
    strategy: str = ""
    comment: str = ""
    expiry: Optional[datetime] = None


@dataclass
class OrderResult:
    ok: bool
    retcode: int = 0
    message: str = ""
    order_ticket: int = 0
    position_ticket: int = 0
    fill_price: float = 0.0
    volume: float = 0.0
    time: Optional[datetime] = None
    retryable: bool = False      # True only when the broker definitely did NOT execute
    uncertain: bool = False      # True when execution status is unknown (timeout etc.)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = self.time.isoformat() if self.time else None
        return d


@dataclass
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    time_open: datetime
    price_current: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""


@dataclass
class PendingOrder:
    ticket: int
    symbol: str
    side: Side
    order_type: OrderType
    volume: float
    price: float
    sl: float
    tp: float
    time_setup: datetime
    magic: int = 0
    comment: str = ""
    expiry: Optional[datetime] = None


@dataclass
class ClosedTrade:
    """A completed round trip."""

    position_ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    price_close: float
    time_open: datetime
    time_close: datetime
    profit: float                # net of commission and swap, account currency
    commission: float = 0.0
    swap: float = 0.0
    sl: float = 0.0              # initial stop
    tp: float = 0.0
    risk_amount: float = 0.0     # account currency risked at entry
    strategy: str = ""
    exit_reason: str = ""
    client_id: str = ""
    magic: int = 0
    comment: str = ""

    @property
    def r_multiple(self) -> float:
        return self.profit / self.risk_amount if self.risk_amount > 0 else 0.0
