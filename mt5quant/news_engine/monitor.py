"""News monitor: turns the economic calendar into trading restrictions.

For a symbol, the relevant events are those of its two currencies. Around a
high-impact event the monitor:

* blocks NEW entries from ``block_minutes_before`` to ``block_minutes_after``
* reduces position size from ``reduce_risk_minutes_before`` onward
* optionally asks the engine to close positions ahead of tier-1 releases

If the calendar cannot be refreshed and the stored data is stale, the monitor
fails CLOSED (no new entries) rather than assuming a quiet calendar.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from ..core.models import UTC
from .events import HIGH, EconomicEvent, is_tier1
from .providers import CalendarProvider, ProviderError

log = logging.getLogger(__name__)

LAST_OK_KEY = "news_last_ok"


@dataclass
class NewsState:
    blocked: bool = False
    reason: str = ""
    risk_factor: float = 1.0
    close_positions: bool = False
    next_event: Optional[EconomicEvent] = None
    notes: list[str] = field(default_factory=list)

    def describe(self, now: datetime) -> str:
        if self.next_event is None:
            return self.reason or "no high-impact event scheduled in the look-ahead window"
        e = self.next_event
        mins = (e.time - now).total_seconds() / 60.0
        when = f"in {mins:.0f} min" if mins >= 0 else f"{-mins:.0f} min ago"
        status = "confirmed schedule" if e.confirmed else "UNCONFIRMED"
        return f"{e.currency} {e.title} {when} ({e.impact} impact, {status}, source: {e.source})"


class NewsMonitor:
    def __init__(self, cfg, db, providers: list[CalendarProvider], enforce_freshness: bool = True) -> None:
        self.cfg = cfg
        self.db = db
        self.providers = providers
        self.enforce_freshness = enforce_freshness
        self._last_attempt: Optional[datetime] = None

    # ---------------------------------------------------------------- refresh
    def refresh(self, now: datetime, force: bool = False) -> bool:
        """Fetch upcoming events from every provider. Returns True if at
        least one provider succeeded."""
        if not self.cfg.enabled:
            return True
        if not force and self._last_attempt and now - self._last_attempt < timedelta(
                minutes=self.cfg.refresh_minutes):
            return True
        self._last_attempt = now
        ok = False
        for p in self.providers:
            try:
                events = p.fetch(now - timedelta(days=1), now + timedelta(days=8))
            except ProviderError as exc:
                log.warning("calendar provider %s failed: %s", p.name, exc)
                continue
            except Exception:
                log.exception("calendar provider %s crashed", p.name)
                continue
            self.db.upsert_news([e.to_row() for e in events])
            ok = True
            log.info("calendar: %d events from %s", len(events), p.name)
        if ok:
            self.db.set_state(LAST_OK_KEY, now.isoformat())
        return ok

    def last_ok(self) -> Optional[datetime]:
        raw = self.db.get_state(LAST_OK_KEY)
        return datetime.fromisoformat(raw) if raw else None

    def is_stale(self, now: datetime) -> bool:
        last = self.last_ok()
        return last is None or now - last > timedelta(hours=self.cfg.stale_after_hours)

    # ------------------------------------------------------------------ query
    def events(self, start: datetime, end: datetime, currencies: Optional[set[str]] = None,
               min_impact: str = HIGH) -> list[EconomicEvent]:
        order = {"low": 0, "medium": 1, "high": 2}
        out = []
        for r in self.db.news_between(start, end):
            e = EconomicEvent.from_row(r)
            if order[e.impact] < order[min_impact]:
                continue
            if currencies and e.currency not in currencies and e.currency != "ALL":
                continue
            out.append(e)
        return out

    def _blackout(self, now: datetime, currencies: set[str]) -> Optional[str]:
        for b in self.cfg.manual_blackouts:
            start = datetime.fromisoformat(b.start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(b.end.replace("Z", "+00:00"))
            start = start if start.tzinfo else start.replace(tzinfo=UTC)
            end = end if end.tzinfo else end.replace(tzinfo=UTC)
            if start <= now <= end and (not b.currencies or currencies & {c.upper() for c in b.currencies}):
                return b.reason
        return None

    def state(self, currencies: set[str], now: datetime) -> NewsState:
        """Restrictions for an instrument whose currencies are ``currencies``."""
        cfg = self.cfg
        st = NewsState()
        if not cfg.enabled:
            st.reason = "news filter disabled"
            return st
        manual = self._blackout(now, currencies)
        if manual:
            return NewsState(blocked=True, reason=f"manual blackout: {manual}", risk_factor=0.0)
        if self.enforce_freshness and cfg.fail_closed and self.is_stale(now):
            return NewsState(blocked=True, risk_factor=0.0,
                             reason="economic calendar unavailable/stale - entries suspended (fail-closed)")
        before = timedelta(minutes=max(cfg.block_minutes_before, cfg.reduce_risk_minutes_before,
                                       cfg.close_before_tier1_minutes))
        after = timedelta(minutes=cfg.block_minutes_after)
        evs = self.events(now - after, now + before, currencies)
        upcoming = [e for e in evs if e.time >= now]
        st.next_event = min(upcoming, key=lambda e: e.time) if upcoming else (max(evs, key=lambda e: e.time)
                                                                             if evs else None)
        for e in evs:
            mins = (e.time - now).total_seconds() / 60.0
            if -cfg.block_minutes_after <= mins <= cfg.block_minutes_before:
                st.blocked = True
                st.risk_factor = 0.0
                st.reason = f"high-impact event window: {e.currency} {e.title}"
                st.next_event = e
            elif 0 < mins <= cfg.reduce_risk_minutes_before and not st.blocked:
                st.risk_factor = min(st.risk_factor, cfg.reduce_risk_factor)
                st.notes.append(f"reduced size ahead of {e.currency} {e.title}")
            if cfg.close_before_tier1_minutes and 0 < mins <= cfg.close_before_tier1_minutes and is_tier1(e.title):
                st.close_positions = True
        return st

    def upcoming(self, now: datetime, hours: int = 48) -> list[dict]:
        return [e.to_row() | {"category": e.category}
                for e in self.events(now - timedelta(hours=1), now + timedelta(hours=hours), None, "medium")]


def blackout_mask(index: pd.DatetimeIndex, events: list[EconomicEvent], currencies: set[str],
                  minutes_before: int, minutes_after: int) -> np.ndarray:
    """Vectorised entry blackout for backtests with a historical calendar.
    ``index`` holds the times at which orders would be sent."""
    mask = np.zeros(len(index), dtype=bool)
    times = index.values
    for e in events:
        if e.impact != HIGH or (e.currency not in currencies and e.currency != "ALL"):
            continue
        t = np.datetime64(pd.Timestamp(e.time).tz_convert("UTC").tz_localize(None))
        naive = pd.DatetimeIndex(times).tz_localize(None) if index.tz is None else index.tz_convert(
            "UTC").tz_localize(None)
        lo = t - np.timedelta64(minutes_before, "m")
        hi = t + np.timedelta64(minutes_after, "m")
        mask |= (naive.values >= lo) & (naive.values <= hi)
    return mask
