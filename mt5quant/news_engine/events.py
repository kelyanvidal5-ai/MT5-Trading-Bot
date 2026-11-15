"""Economic-event model and impact classification."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from ..core.models import UTC

HIGH, MEDIUM, LOW = "high", "medium", "low"

# Tier-1 events: scheduled releases that routinely move FX sharply. An event
# matching these is treated as high impact even if a provider rates it lower.
TIER1 = [
    (r"\b(interest rate|rate decision|rate statement|cash rate|official bank rate|main refinancing|"
     r"monetary policy (statement|decision|report)|policy rate)\b", "rate_decision"),
    (r"\b(fomc|federal funds|fed (chair|press)|federal reserve)\b", "fed"),
    (r"\b(ecb (press|president|monetary)|lagarde)\b", "ecb"),
    (r"\b(non-?farm|nfp|payrolls?)\b", "nfp"),
    (r"\b(cpi|consumer price|inflation rate|pce price|core pce)\b", "inflation"),
    (r"\b(ppi|producer price)\b", "ppi"),
    (r"\b(gdp|gross domestic product)\b", "gdp"),
    (r"\b(unemployment rate|employment change|jobless claims)\b", "employment"),
    (r"\b(speaks|speech|testif|press conference)\b", "central_bank_speech"),
]
_TIER1 = [(re.compile(p, re.I), cat) for p, cat in TIER1]
# Speeches matter only when given by the top official of a major central bank.
_TOP_SPEAKERS = re.compile(r"\b(powell|lagarde|bailey|ueda|macklem|bullock|orr|jordan|schlegel|"
                           r"fed chair|ecb president|boe gov|boj gov)\b", re.I)


def classify(title: str) -> str:
    for pat, cat in _TIER1:
        if pat.search(title):
            if cat == "central_bank_speech" and not _TOP_SPEAKERS.search(title):
                return "speech"
            return cat
    return "other"


def is_tier1(title: str) -> bool:
    return classify(title) not in ("other", "speech")


@dataclass
class EconomicEvent:
    time: datetime
    currency: str
    title: str
    impact: str                    # high | medium | low
    source: str
    confirmed: bool = True         # True = scheduled release from a calendar provider / official source
    actual: str = ""
    forecast: str = ""
    previous: str = ""

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            self.time = self.time.replace(tzinfo=UTC)
        self.time = self.time.astimezone(UTC)
        self.currency = self.currency.upper().strip()
        if self.impact not in (HIGH, MEDIUM, LOW):
            self.impact = LOW
        if is_tier1(self.title) and self.impact != HIGH and self.confirmed:
            self.impact = HIGH

    @property
    def id(self) -> str:
        raw = f"{self.time.isoformat()}|{self.currency}|{self.title.lower()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @property
    def category(self) -> str:
        return classify(self.title)

    def to_row(self) -> dict:
        d = asdict(self)
        d["time"] = self.time.isoformat()
        d["id"] = self.id
        return d

    @classmethod
    def from_row(cls, r: dict) -> "EconomicEvent":
        return cls(time=datetime.fromisoformat(r["time"]), currency=r["currency"], title=r["title"],
                   impact=r["impact"], source=r["source"], confirmed=bool(r["confirmed"]),
                   actual=r.get("actual", ""), forecast=r.get("forecast", ""), previous=r.get("previous", ""))
