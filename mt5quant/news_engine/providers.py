"""Economic-calendar providers.

Only documented, legitimate sources are used, and every event keeps its
``source`` so its provenance is always visible:

* ``faireconomy``  Forex Factory's public weekly calendar export (JSON). No
                   key required. Covers the current week only.
* ``finnhub``      Finnhub economic calendar API (needs FINNHUB_API_KEY).
* ``file``         A local CSV/JSON you maintain or export yourself; also
                   used for historical calendars in backtests.
* ``OfficialFeeds``RSS press feeds of the Federal Reserve and the ECB. These
                   are primary sources, shown as headlines; they inform the
                   operator and never trigger trades on their own.

Nothing here generates, paraphrases or predicts news: providers only parse
what the source returned. A provider that fails returns an error - it never
fabricates an empty "all clear".
"""
from __future__ import annotations

import csv
import io
import json
import logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional

from ..core.config import Secrets
from ..core.models import UTC
from .events import HIGH, LOW, MEDIUM, EconomicEvent

log = logging.getLogger(__name__)

Fetch = Callable[[str, dict], str]          # (url, params) -> response text


class ProviderError(RuntimeError):
    pass


def http_fetch(url: str, params: dict) -> str:
    import requests

    try:
        r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "mt5quant-calendar/1.0"})
    except requests.RequestException as exc:
        raise ProviderError(f"request failed: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise ProviderError(f"HTTP {r.status_code}")
    return r.text


class CalendarProvider(ABC):
    name = "provider"

    @abstractmethod
    def fetch(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        """Events in [start, end]. Raises ProviderError on failure."""


_IMPACT = {"high": HIGH, "medium": MEDIUM, "low": LOW, "3": HIGH, "2": MEDIUM, "1": LOW}


class FairEconomyProvider(CalendarProvider):
    name = "faireconomy"
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self, fetch: Fetch = http_fetch) -> None:
        self._fetch = fetch

    def fetch(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        try:
            rows = json.loads(self._fetch(self.URL, {}))
        except ValueError as exc:
            raise ProviderError("invalid JSON from faireconomy") from exc
        if not isinstance(rows, list):
            raise ProviderError("unexpected payload from faireconomy")
        out = []
        for r in rows:
            try:
                when = datetime.fromisoformat(str(r["date"]))
                impact = _IMPACT.get(str(r.get("impact", "")).lower())
                if impact is None:                 # "Holiday" / "Non-Economic"
                    continue
                ev = EconomicEvent(time=when, currency=str(r["country"]), title=str(r["title"]), impact=impact,
                                   source=self.name, forecast=str(r.get("forecast") or ""),
                                   previous=str(r.get("previous") or ""), actual=str(r.get("actual") or ""))
            except (KeyError, ValueError, TypeError):
                continue
            if start <= ev.time <= end:
                out.append(ev)
        return out


_COUNTRY_CCY = {"US": "USD", "EU": "EUR", "EMU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
                "GB": "GBP", "UK": "GBP", "JP": "JPY", "CH": "CHF", "CA": "CAD", "AU": "AUD", "NZ": "NZD",
                "CN": "CNY"}


class FinnhubProvider(CalendarProvider):
    name = "finnhub"
    URL = "https://finnhub.io/api/v1/calendar/economic"

    def __init__(self, fetch: Fetch = http_fetch, api_key: Optional[str] = None) -> None:
        self._fetch = fetch
        self._key = api_key

    def fetch(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        key = self._key or Secrets.get("FINNHUB_API_KEY")
        if not key:
            raise ProviderError("FINNHUB_API_KEY is not set")
        try:
            data = json.loads(self._fetch(self.URL, {"from": start.strftime("%Y-%m-%d"),
                                                     "to": end.strftime("%Y-%m-%d"), "token": key}))
            rows = data["economicCalendar"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("unexpected payload from finnhub") from exc
        out = []
        for r in rows:
            ccy = _COUNTRY_CCY.get(str(r.get("country", "")).upper())
            if not ccy:
                continue
            try:
                when = datetime.strptime(str(r["time"])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            ev = EconomicEvent(time=when, currency=ccy, title=str(r.get("event", "")),
                               impact=_IMPACT.get(str(r.get("impact", "")).lower(), LOW), source=self.name,
                               actual=str(r.get("actual") or ""), forecast=str(r.get("estimate") or ""),
                               previous=str(r.get("prev") or ""))
            if start <= ev.time <= end:
                out.append(ev)
        return out


class FileProvider(CalendarProvider):
    """CSV (time,currency,title,impact[,actual,forecast,previous]) or a JSON
    list of the same fields. Times are ISO-8601; naive times are UTC."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        if not self.path.exists():
            raise ProviderError(f"calendar file not found: {self.path}")
        text = self.path.read_text()
        try:
            rows = json.loads(text) if self.path.suffix == ".json" else list(csv.DictReader(io.StringIO(text)))
        except ValueError as exc:
            raise ProviderError("calendar file is not valid") from exc
        out = []
        for r in rows:
            try:
                ev = EconomicEvent(time=datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00")),
                                   currency=str(r["currency"]), title=str(r["title"]),
                                   impact=_IMPACT.get(str(r.get("impact", "low")).lower(), LOW), source=self.name,
                                   actual=str(r.get("actual") or ""), forecast=str(r.get("forecast") or ""),
                                   previous=str(r.get("previous") or ""))
            except (KeyError, ValueError, TypeError):
                continue
            if start <= ev.time <= end:
                out.append(ev)
        return out


class OfficialFeeds:
    """Headlines from primary sources. ``confirmed`` is True only because the
    publisher is the institution itself; nothing is inferred from the text."""

    FEEDS = {
        "federalreserve.gov": ("USD", "https://www.federalreserve.gov/feeds/press_all.xml"),
        "ecb.europa.eu": ("EUR", "https://www.ecb.europa.eu/rss/press.html"),
    }

    def __init__(self, fetch: Fetch = http_fetch) -> None:
        self._fetch = fetch

    def headlines(self, max_age: timedelta = timedelta(days=2), now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.now(UTC)
        out: list[dict] = []
        for source, (ccy, url) in self.FEEDS.items():
            try:
                root = ET.fromstring(self._fetch(url, {}))
            except (ProviderError, ET.ParseError) as exc:
                log.warning("headline feed %s unavailable: %s", source, exc)
                continue
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                try:
                    when = parsedate_to_datetime(item.findtext("pubDate") or "")
                    when = when if when.tzinfo else when.replace(tzinfo=UTC)
                except (TypeError, ValueError):
                    continue
                if title and now - when <= max_age:
                    out.append({"time": when.astimezone(UTC).isoformat(), "currency": ccy, "title": title,
                                "source": source, "confirmed": True, "link": item.findtext("link") or ""})
        return sorted(out, key=lambda h: h["time"], reverse=True)


def build_providers(names: list[str], calendar_file: Optional[str] = None,
                    fetch: Fetch = http_fetch) -> list[CalendarProvider]:
    out: list[CalendarProvider] = []
    for n in names:
        if n == "faireconomy":
            out.append(FairEconomyProvider(fetch))
        elif n == "finnhub":
            out.append(FinnhubProvider(fetch))
        elif n == "file":
            if not calendar_file:
                raise ValueError("news.calendar_file must be set for the 'file' provider")
            out.append(FileProvider(calendar_file))
        else:
            raise ValueError(f"unknown news provider '{n}'")
    return out
