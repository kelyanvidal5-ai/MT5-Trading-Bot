"""The risk manager: the single gate every trade must pass.

It owns three things:

1. *Account-level circuit breakers* - daily loss, weekly loss, maximum
   drawdown, abnormal-behaviour breaker and the emergency kill switch. Their
   state is persisted, so a restart cannot "forget" a breach.
2. *Pre-trade checks* - everything that must be true before a single order
   is sent (stop present and valid, reward/risk, spread, margin, position
   and correlation limits, worst-case loss headroom).
3. *Position sizing* - fixed-fractional, only ever scaled DOWN.

Nothing outside this module can raise risk: strategies propose, the AI
engine scores, the risk manager disposes.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..core.models import UTC, AccountInfo, ClosedTrade, Position, Side, Signal, SymbolSpec, Tick
from .limits import RiskLimits
from .sizing import position_size

log = logging.getLogger(__name__)

STATE_KEY = "risk_state"
KILL_KEY = "kill_switch"


@dataclass
class RiskDecision:
    approved: bool
    volume: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    entry_price: float = 0.0
    scale: float = 1.0
    scale_reason: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskState:
    day_key: str = ""
    day_start_equity: float = 0.0
    week_key: str = ""
    week_start_equity: float = 0.0
    peak_equity: float = 0.0
    suspended: bool = False                 # drawdown / abnormal behaviour: manual reset required
    suspend_reason: str = ""
    suspended_at: str = ""
    daily_halt_key: str = ""                # day on which the daily limit was hit
    weekly_halt_key: str = ""
    consecutive_losses: int = 0
    cooldown_until: str = ""
    order_errors: list[str] = field(default_factory=list)
    last_equity: float = 0.0


def remaining_risk(p: Position, spec: SymbolSpec) -> float:
    """Account-currency loss if the position's CURRENT stop is hit (0 when
    the stop already locks in break-even or better)."""
    if p.sl <= 0:
        return float("inf")
    adverse = (p.price_open - p.sl) * p.side.sign
    return max(adverse, 0.0) * spec.value_per_price_unit() * p.volume


class RiskManager:
    def __init__(self, limits: RiskLimits, db, day_reset_hour_utc: int = 0, quiet: bool = False) -> None:
        self.quiet = quiet                    # backtests: do not log every simulated breach
        self.limits = limits
        self.db = db
        self.reset_hour = day_reset_hour_utc
        raw = db.get_state(STATE_KEY)
        self.state = RiskState(**{k: v for k, v in (raw or {}).items() if k in RiskState.__dataclass_fields__})

    # ---------------------------------------------------------------- helpers
    def _save(self) -> None:
        self.db.set_state(STATE_KEY, asdict(self.state))

    def _day_key(self, now: datetime) -> str:
        return (now.astimezone(UTC) - timedelta(hours=self.reset_hour)).strftime("%Y-%m-%d")

    def _week_key(self, now: datetime) -> str:
        shifted = now.astimezone(UTC) - timedelta(hours=self.reset_hour)
        iso = shifted.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    # ------------------------------------------------------------ kill switch
    def kill_switch(self) -> dict:
        return self.db.get_state(KILL_KEY, {"active": False, "reason": "", "actor": "", "time": ""})

    def emergency_stop(self, reason: str, actor: str = "engine", now: Optional[datetime] = None) -> None:
        self.db.set_state(KILL_KEY, {"active": True, "reason": reason, "actor": actor,
                                     "time": (now or datetime.now(UTC)).isoformat(), "handled": False})
        self.db.audit("emergency_stop", {"reason": reason}, actor=actor)
        log.critical("EMERGENCY STOP by %s: %s", actor, reason)

    def mark_kill_handled(self) -> None:
        ks = self.kill_switch()
        ks["handled"] = True
        self.db.set_state(KILL_KEY, ks)

    def resume(self, actor: str, account: Optional[AccountInfo] = None) -> None:
        """Manual, audited resumption after an emergency stop or suspension.
        The drawdown high-water mark restarts from current equity so the
        operator's decision is not immediately overridden."""
        self.db.set_state(KILL_KEY, {"active": False, "reason": "", "actor": actor, "time": "", "handled": True})
        self.state.suspended, self.state.suspend_reason, self.state.suspended_at = False, "", ""
        self.state.order_errors = []
        self.state.consecutive_losses, self.state.cooldown_until = 0, ""
        if account is not None:
            self.state.peak_equity = account.equity
        elif self.state.last_equity > 0:
            self.state.peak_equity = self.state.last_equity
        self._save()
        self.db.audit("trading_resumed", {"peak_equity_reset_to": self.state.peak_equity}, actor=actor)

    def _suspend(self, reason: str, now: datetime) -> None:
        if not self.state.suspended:
            self.state.suspended, self.state.suspend_reason = True, reason
            self.state.suspended_at = now.isoformat()
            self.db.audit("trading_suspended", {"reason": reason})
            if not self.quiet:
                log.error("TRADING SUSPENDED: %s", reason)

    # --------------------------------------------------------- account update
    def update(self, account: AccountInfo, now: datetime, persist: bool = True) -> list[tuple[str, str]]:
        """Roll daily/weekly anchors, track the high-water mark and trip
        circuit breakers. Returns newly raised (event, message) pairs."""
        s, events = self.state, []
        equity = account.equity
        day, week = self._day_key(now), self._week_key(now)
        if s.day_key != day:
            s.day_key, s.day_start_equity = day, equity
        if s.week_key != week:
            s.week_key, s.week_start_equity = week, equity
        if s.peak_equity <= 0:
            s.peak_equity = max(equity, account.balance)
        s.peak_equity = max(s.peak_equity, equity)
        s.last_equity = equity

        if s.daily_halt_key != day and self.daily_loss_pct(equity) >= self.limits.max_daily_loss_pct:
            s.daily_halt_key = day
            msg = (f"daily loss {self.daily_loss_pct(equity):.2f}% reached the limit "
                   f"{self.limits.max_daily_loss_pct:.2f}% - no new positions until the next trading day")
            self.db.audit("daily_loss_limit", {"loss_pct": self.daily_loss_pct(equity)})
            events.append(("daily_loss_limit", msg))
        if s.weekly_halt_key != week and self.weekly_loss_pct(equity) >= self.limits.max_weekly_loss_pct:
            s.weekly_halt_key = week
            msg = (f"weekly loss {self.weekly_loss_pct(equity):.2f}% reached the limit "
                   f"{self.limits.max_weekly_loss_pct:.2f}% - no new positions until next week")
            self.db.audit("weekly_loss_limit", {"loss_pct": self.weekly_loss_pct(equity)})
            events.append(("weekly_loss_limit", msg))
        dd = self.drawdown_pct(equity)
        if not s.suspended and dd >= self.limits.max_drawdown_pct:
            msg = (f"drawdown {dd:.2f}% from the equity peak {s.peak_equity:.2f} reached the limit "
                   f"{self.limits.max_drawdown_pct:.2f}% - trading suspended until manually resumed")
            self._suspend(msg, now)
            events.append(("max_drawdown", msg))
        if account.margin > 0 and 0 < account.margin_level < self.limits.min_margin_level_pct:
            events.append(("margin_warning", f"margin level {account.margin_level:.0f}% is below "
                                             f"{self.limits.min_margin_level_pct:.0f}%"))
        if persist or events:
            self._save()
        return events

    def daily_loss_pct(self, equity: float) -> float:
        base = self.state.day_start_equity
        return max(0.0, (base - equity) / base * 100.0) if base > 0 else 0.0

    def weekly_loss_pct(self, equity: float) -> float:
        base = self.state.week_start_equity
        return max(0.0, (base - equity) / base * 100.0) if base > 0 else 0.0

    def drawdown_pct(self, equity: float) -> float:
        peak = self.state.peak_equity
        return max(0.0, (peak - equity) / peak * 100.0) if peak > 0 else 0.0

    # ------------------------------------------------------------------ events
    def on_trade_closed(self, trade: ClosedTrade, now: datetime) -> Optional[str]:
        s = self.state
        if trade.profit < 0:
            s.consecutive_losses += 1
            if s.consecutive_losses >= self.limits.max_consecutive_losses:
                until = now + timedelta(minutes=self.limits.cooldown_minutes_after_loss_streak)
                s.cooldown_until = until.isoformat()
                s.consecutive_losses = 0
                self._save()
                self.db.audit("loss_streak_cooldown", {"until": s.cooldown_until})
                return (f"{self.limits.max_consecutive_losses} consecutive losses - cooling down until "
                        f"{until:%Y-%m-%d %H:%M} UTC")
        else:
            s.consecutive_losses = 0
        self._save()
        return None

    def on_order_error(self, now: datetime, detail: str = "") -> Optional[str]:
        """Abnormal-behaviour breaker: too many failed orders => suspend."""
        s = self.state
        cutoff = now - timedelta(hours=1)
        s.order_errors = [t for t in s.order_errors if datetime.fromisoformat(t) >= cutoff] + [now.isoformat()]
        msg = None
        if len(s.order_errors) >= self.limits.max_order_errors_per_hour and not s.suspended:
            msg = (f"{len(s.order_errors)} order errors within one hour ({detail}) - abnormal behaviour, "
                   "trading suspended until manually resumed")
            self._suspend(msg, now)
        self._save()
        return msg

    # ------------------------------------------------------------ gate checks
    def can_open(self, now: datetime) -> tuple[bool, list[str]]:
        s, reasons = self.state, []
        ks = self.kill_switch()
        if ks.get("active"):
            reasons.append(f"emergency stop active: {ks.get('reason', '')}")
        if s.suspended:
            reasons.append(f"trading suspended: {s.suspend_reason}")
        if s.daily_halt_key and s.daily_halt_key == self._day_key(now):
            reasons.append("daily loss limit reached")
        if s.weekly_halt_key and s.weekly_halt_key == self._week_key(now):
            reasons.append("weekly loss limit reached")
        if s.cooldown_until and now < datetime.fromisoformat(s.cooldown_until):
            reasons.append(f"loss-streak cool-down until {s.cooldown_until[:16]}")
        return (not reasons), reasons

    def risk_scale(self, equity: float, confidence: float, min_confidence: float,
                   news_factor: float = 1.0) -> tuple[float, str]:
        """Dynamic sizing multiplier in (0, 1]. It can only reduce risk."""
        parts, scale = [], 1.0
        dd = self.drawdown_pct(equity)
        if dd > 0:
            f = max(0.5, 1.0 - 0.5 * dd / self.limits.max_drawdown_pct)
            if f < 0.999:
                scale *= f
                parts.append(f"drawdown {dd:.1f}% x{f:.2f}")
        span = max(0.85 - min_confidence, 1e-6)
        f = min(1.0, max(0.6, 0.6 + 0.4 * (confidence - min_confidence) / span))
        if f < 0.999:
            scale *= f
            parts.append(f"confidence {confidence:.2f} x{f:.2f}")
        if news_factor < 0.999:
            scale *= max(news_factor, 0.0)
            parts.append(f"pre-news x{news_factor:.2f}")
        return min(scale, 1.0), ", ".join(parts)

    def evaluate(self, signal: Signal, account: AccountInfo, positions: list[Position], tick: Tick,
                 spec: SymbolSpec, specs: dict[str, SymbolSpec], now: datetime, atr: float,
                 trades_today: int, scale: float = 1.0, scale_reason: str = "") -> RiskDecision:
        """Full pre-trade check. Returns an approved decision with a volume,
        or a rejection listing every failed rule."""
        L = self.limits
        ok, reasons = self.can_open(now)
        side = signal.side
        entry = tick.ask if side is Side.BUY else tick.bid
        d = RiskDecision(approved=False, entry_price=entry, scale=min(max(scale, 0.0), 1.0),
                         scale_reason=scale_reason, reasons=reasons)

        if not account.trade_allowed:
            reasons.append("trading is disabled on the account")
        if not spec.trade_allowed:
            reasons.append(f"trading is disabled for {spec.name}")
        # --- stop / target sanity (every position must have a stop) ---------
        if not signal.sl or signal.sl <= 0:
            reasons.append("signal has no stop-loss")
            return d
        if not signal.tp or signal.tp <= 0:
            reasons.append("signal has no take-profit")
            return d
        risk_dist = (entry - signal.sl) * side.sign
        reward_dist = (signal.tp - entry) * side.sign
        min_dist = max(spec.stops_level * spec.point, 2.0 * tick.spread)
        if risk_dist <= 0:
            reasons.append("stop-loss is on the wrong side of the market")
            return d
        if reward_dist <= 0:
            reasons.append("take-profit is on the wrong side of the market")
            return d
        if risk_dist < min_dist:
            reasons.append("stop-loss is too close (broker stop level / spread)")
        if reward_dist / risk_dist < L.min_reward_risk:
            reasons.append(f"reward/risk {reward_dist / risk_dist:.2f} below minimum {L.min_reward_risk:.2f}")
        drift = abs(entry - signal.ref_price) - tick.spread
        if drift > 0.5 * signal.risk_distance:
            reasons.append("price moved away from the signal level")
        # --- market quality ---------------------------------------------------
        if atr > 0 and tick.spread / atr > L.max_spread_atr_ratio:
            reasons.append(f"spread is {tick.spread / atr:.0%} of ATR (max {L.max_spread_atr_ratio:.0%})")
        # --- exposure limits --------------------------------------------------
        if len(positions) >= L.max_open_positions:
            reasons.append(f"maximum open positions ({L.max_open_positions}) reached")
        if trades_today >= L.max_trades_per_day:
            reasons.append(f"maximum trades per day ({L.max_trades_per_day}) reached")
        same = [p for p in positions if p.symbol == signal.symbol]
        if len(same) >= L.max_positions_per_symbol:
            reasons.append(f"already {len(same)} position(s) on {signal.symbol}")
        for p in same:
            if p.side is not side:
                reasons.append("opposite position open on this symbol (no hedging)")
            elif (p.sl - p.price_open) * p.side.sign < 0:
                reasons.append("existing position is not risk-free: adding would be averaging in")
        # --- sizing -------------------------------------------------------------
        # Size for the WORST fill the order may receive (entry plus the maximum
        # slippage tolerated by the order's deviation), so the risk budget holds
        # even when the fill slips.
        risk_pct = L.risk_per_trade_pct * d.scale
        worst_entry = entry + side.sign * L.max_slippage_points * spec.point
        volume, risk_amount = position_size(account.equity, risk_pct, worst_entry, signal.sl, spec)
        if volume <= 0:
            reasons.append("stop distance too wide for the risk budget at minimum lot size")
            return d
        assert risk_amount <= account.equity * L.risk_per_trade_pct / 100.0 * 1.0001, "risk budget exceeded"
        # --- portfolio risk -----------------------------------------------------
        open_risk = 0.0
        exposure: dict[str, float] = {}
        for p in positions:
            ps = specs.get(p.symbol)
            r = remaining_risk(p, ps) if ps else float("inf")
            open_risk += r
            _add_exposure(exposure, ps, p.symbol, p.side, r)
        if open_risk == float("inf"):
            reasons.append("an open position has no stop-loss: refusing to add risk")
        else:
            total_pct = (open_risk + risk_amount) / account.equity * 100.0
            if total_pct > L.max_total_open_risk_pct + 1e-9:
                reasons.append(f"total open risk would be {total_pct:.2f}% (max {L.max_total_open_risk_pct:.2f}%)")
            _add_exposure(exposure, spec, signal.symbol, side, risk_amount)
            worst_ccy, worst = max(exposure.items(), key=lambda kv: abs(kv[1]))
            worst_pct = abs(worst) / account.equity * 100.0
            if worst_pct > L.max_currency_exposure_risk_pct + 1e-9:
                reasons.append(f"correlated exposure: net {worst_ccy} risk would be {worst_pct:.2f}% "
                               f"(max {L.max_currency_exposure_risk_pct:.2f}%)")
            # Worst case today: realised loss + every open stop + this stop.
            headroom = L.max_daily_loss_pct - self.daily_loss_pct(account.equity)
            if (open_risk + risk_amount) / account.equity * 100.0 > headroom + 1e-9:
                reasons.append("worst-case loss would breach the daily loss limit")
        # --- margin ---------------------------------------------------------------
        leverage = max(account.leverage, 1)
        need = volume * spec.value_per_price_unit() * entry * spec.margin_rate / leverage
        if need > account.margin_free:
            reasons.append("insufficient free margin")
        used_pct = (account.margin + need) / account.equity * 100.0 if account.equity > 0 else 100.0
        if used_pct > L.max_margin_usage_pct:
            reasons.append(f"margin usage would be {used_pct:.0f}% of equity (max {L.max_margin_usage_pct:.0f}%)")
        level = account.equity / (account.margin + need) * 100.0 if account.margin + need > 0 else float("inf")
        if level < L.min_margin_level_pct:
            reasons.append(f"margin level would fall to {level:.0f}% (min {L.min_margin_level_pct:.0f}%)")

        d.volume, d.risk_amount = volume, risk_amount
        d.risk_pct = risk_amount / account.equity * 100.0
        d.approved = not reasons
        if not d.approved:
            d.volume = 0.0
        return d

    # ------------------------------------------------------------------ status
    def snapshot(self, equity: Optional[float] = None) -> dict:
        s = self.state
        eq = equity if equity is not None else s.last_equity
        return {
            "limits": self.limits.model_dump(),
            "daily_loss_pct": self.daily_loss_pct(eq), "weekly_loss_pct": self.weekly_loss_pct(eq),
            "drawdown_pct": self.drawdown_pct(eq), "peak_equity": s.peak_equity,
            "day_start_equity": s.day_start_equity, "week_start_equity": s.week_start_equity,
            "suspended": s.suspended, "suspend_reason": s.suspend_reason,
            "daily_halted": bool(s.daily_halt_key and s.daily_halt_key == s.day_key),
            "weekly_halted": bool(s.weekly_halt_key and s.weekly_halt_key == s.week_key),
            "cooldown_until": s.cooldown_until, "consecutive_losses": s.consecutive_losses,
            "kill_switch": self.kill_switch(),
        }


def _add_exposure(exposure: dict[str, float], spec: Optional[SymbolSpec], symbol: str, side: Side,
                  risk: float) -> None:
    """Long EURUSD = long EUR / short USD. Risk is attributed to both legs so
    EURUSD-long + GBPUSD-long count as stacked USD-short exposure."""
    if risk == float("inf"):
        return
    base = (spec.currency_base if spec else "") or symbol[:3]
    quote = (spec.currency_profit if spec else "") or symbol[3:6] or symbol
    exposure[base] = exposure.get(base, 0.0) + side.sign * risk
    exposure[quote] = exposure.get(quote, 0.0) - side.sign * risk
