"""Market calendar: trading weeks, sessions, rollover and holidays.

Forex is a 24/5 market that opens Sunday 17:00 New York and closes Friday
17:00 New York (DST-aware via zoneinfo). The calendar here is the *first*
gate; the engine additionally requires a fresh tick from the terminal, which
is the ground truth for broker-specific hours of indices, metals and CFDs.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..core.models import UTC

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")
SYDNEY = ZoneInfo("Australia/Sydney")

_CLOSE = time(17, 0)

# (month, day) -> market closed worldwide for FX/CFDs
FIXED_CLOSED = {(1, 1), (12, 25)}
# (month, day) -> open but structurally illiquid: no new entries
FIXED_THIN = {(12, 24), (12, 26), (12, 31), (1, 2)}

SESSIONS = {
    # name: (zone, local open, local close)
    "sydney": (SYDNEY, time(7, 0), time(16, 0)),
    "tokyo": (TOKYO, time(9, 0), time(18, 0)),
    "london": (LONDON, time(8, 0), time(17, 0)),
    "newyork": (NY, time(8, 0), time(17, 0)),
}


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class MarketCalendar:
    def __init__(self, avoid_rollover_minutes: int = 65, friday_cutoff_hours: float = 2.0,
                 monday_open_delay_minutes: int = 60, holidays: Optional[Iterable[str]] = None) -> None:
        self.avoid_rollover = timedelta(minutes=avoid_rollover_minutes)
        self.friday_cutoff = timedelta(hours=friday_cutoff_hours)
        self.monday_delay = timedelta(minutes=monday_open_delay_minutes)
        self.extra_holidays: set[date] = {date.fromisoformat(h) for h in (holidays or [])}

    # ----------------------------------------------------------- open/closed
    def is_holiday(self, t: datetime) -> bool:
        d = _aware(t).astimezone(NY).date()
        return (d.month, d.day) in FIXED_CLOSED or d in self.extra_holidays

    def is_thin(self, t: datetime) -> bool:
        d = _aware(t).astimezone(NY).date()
        return (d.month, d.day) in FIXED_THIN

    def is_open(self, t: datetime, asset_class: str = "forex") -> bool:
        if asset_class == "crypto":
            return True
        ny = _aware(t).astimezone(NY)
        wd, tm = ny.weekday(), ny.time()      # Monday=0 .. Sunday=6
        if wd == 5:
            return False
        if wd == 6 and tm < _CLOSE:
            return False
        if wd == 4 and tm >= _CLOSE:
            return False
        if self.is_holiday(t):
            return False
        if asset_class in ("metal", "index", "commodity") and time(17, 0) <= tm < time(18, 0):
            return False                       # daily maintenance break
        return True

    def week_close(self, t: datetime) -> datetime:
        """Next Friday 17:00 New York at or after ``t`` (UTC)."""
        ny = _aware(t).astimezone(NY)
        days = (4 - ny.weekday()) % 7
        close = datetime.combine(ny.date() + timedelta(days=days), _CLOSE, tzinfo=NY)
        if close < ny:
            close += timedelta(days=7)
        return close.astimezone(UTC)

    def week_open(self, t: datetime) -> datetime:
        """Most recent Sunday 17:00 New York at or before ``t`` (UTC)."""
        ny = _aware(t).astimezone(NY)
        days = (ny.weekday() - 6) % 7
        opened = datetime.combine(ny.date() - timedelta(days=days), _CLOSE, tzinfo=NY)
        if opened > ny:
            opened -= timedelta(days=7)
        return opened.astimezone(UTC)

    def in_rollover(self, t: datetime) -> bool:
        """Around 17:00 New York spreads widen sharply and swaps are booked."""
        ny = _aware(t).astimezone(NY)
        roll = datetime.combine(ny.date(), _CLOSE, tzinfo=NY)
        return abs((ny - roll).total_seconds()) <= self.avoid_rollover.total_seconds()

    def active_sessions(self, t: datetime) -> list[str]:
        out = []
        for name, (zone, start, end) in SESSIONS.items():
            local = _aware(t).astimezone(zone)
            if local.weekday() < 5 and start <= local.time() < end:
                out.append(name)
        return out

    def entry_allowed(self, t: datetime, asset_class: str = "forex") -> tuple[bool, str]:
        """Whether NEW positions may be opened at ``t``."""
        t = _aware(t)
        if not self.is_open(t, asset_class):
            return False, "market closed"
        if asset_class == "crypto":
            return True, "ok"
        if self.is_thin(t):
            return False, "holiday-thinned liquidity"
        if self.in_rollover(t):
            return False, "daily rollover window (wide spreads)"
        if self.week_close(t) - t <= self.friday_cutoff:
            return False, "too close to weekly close"
        if t - self.week_open(t) < self.monday_delay:
            return False, "weekly open (gap/illiquid)"
        return True, "ok"

    # ------------------------------------------------------------ vectorized
    def entry_mask(self, index: pd.DatetimeIndex, asset_class: str = "forex") -> np.ndarray:
        """Vectorised :meth:`entry_allowed` for backtests (same rules)."""
        if asset_class == "crypto":
            return np.ones(len(index), dtype=bool)
        ny = index.tz_convert(NY)
        wd = ny.weekday.values
        minutes = (ny.hour.values * 60 + ny.minute.values).astype(int)
        close_min = 17 * 60
        open_ = ~((wd == 5) | ((wd == 6) & (minutes < close_min)) | ((wd == 4) & (minutes >= close_min)))
        md = list(zip(ny.month.values, ny.day.values))
        closed = np.array([m in FIXED_CLOSED for m in md])
        thin = np.array([m in FIXED_THIN for m in md])
        if self.extra_holidays:
            dates = ny.date
            closed |= np.array([d in self.extra_holidays for d in dates])
        roll = np.abs(minutes - close_min) <= self.avoid_rollover.total_seconds() / 60
        # minutes since weekly open / until weekly close
        since_open = ((wd - 6) % 7) * 1440 + minutes - close_min
        since_open = np.where(since_open < 0, since_open + 7 * 1440, since_open)
        to_close = 5 * 1440 - since_open
        friday = to_close <= self.friday_cutoff.total_seconds() / 60
        monday = since_open < self.monday_delay.total_seconds() / 60
        mask = open_ & ~closed & ~thin & ~roll & ~friday & ~monday
        if asset_class in ("metal", "index", "commodity"):
            mask &= ~((minutes >= close_min) & (minutes < close_min + 60))
        return mask

    @staticmethod
    def session_mask(index: pd.DatetimeIndex, sessions: Iterable[str]) -> np.ndarray:
        """True where any of the named sessions is active."""
        mask = np.zeros(len(index), dtype=bool)
        for name in sessions:
            zone, start, end = SESSIONS[name]
            loc = index.tz_convert(zone)
            minutes = loc.hour.values * 60 + loc.minute.values
            mask |= (loc.weekday.values < 5) & (minutes >= start.hour * 60 + start.minute) & \
                    (minutes < end.hour * 60 + end.minute)
        return mask
