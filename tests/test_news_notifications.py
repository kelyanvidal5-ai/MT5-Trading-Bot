import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from mt5quant.core.config import Blackout, NewsConfig
from mt5quant.news_engine.events import HIGH, LOW, MEDIUM, EconomicEvent, classify, is_tier1
from mt5quant.news_engine.monitor import NewsMonitor, blackout_mask
from mt5quant.news_engine.providers import (FairEconomyProvider, FileProvider, FinnhubProvider, OfficialFeeds,
                                            ProviderError, build_providers)
from mt5quant.notifications.notifier import Channel, Notifier, TelegramChannel, WebhookChannel

UTC = timezone.utc
NOW = datetime(2024, 3, 12, 11, 0, tzinfo=UTC)

# Shape of Forex Factory's public weekly export (recorded sample, not live data).
FF_SAMPLE = json.dumps([
    {"title": "CPI m/m", "country": "USD", "date": "2024-03-12T08:30:00-04:00", "impact": "High",
     "forecast": "0.4%", "previous": "0.3%"},
    {"title": "German Final CPI m/m", "country": "EUR", "date": "2024-03-12T03:00:00-04:00", "impact": "Low",
     "forecast": "0.4%", "previous": "0.4%"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2024-03-20T00:00:00-04:00", "impact": "Holiday"},
    {"title": "broken row"},
])
FINNHUB_SAMPLE = json.dumps({"economicCalendar": [
    {"country": "US", "event": "Nonfarm Payrolls", "impact": "high", "time": "2024-03-08 13:30:00",
     "actual": 275, "estimate": 200, "prev": 229},
    {"country": "ZZ", "event": "Unknown country", "impact": "high", "time": "2024-03-08 13:30:00"},
]})
RSS_SAMPLE = """<?xml version="1.0"?><rss><channel>
<item><title>Federal Reserve issues FOMC statement</title><pubDate>Tue, 12 Mar 2024 10:00:00 GMT</pubDate>
<link>https://www.federalreserve.gov/x</link></item>
<item><title>Old item</title><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate><link>x</link></item>
</channel></rss>"""


def test_event_classification():
    assert classify("Federal Funds Rate") == "fed" and is_tier1("FOMC Statement")
    assert classify("Non-Farm Employment Change") == "nfp"
    assert classify("Core CPI m/m") == "inflation" and classify("PPI m/m") == "ppi"
    assert classify("Main Refinancing Rate") == "rate_decision"
    assert classify("ECB President Lagarde Speaks") == "ecb"
    assert classify("Fed Chair Powell Speaks") == "fed"
    assert classify("BOE Gov Bailey Speaks") == "central_bank_speech"
    assert classify("FOMC Member Bowman Speaks") == "fed"
    assert classify("MPC Member Mann Speaks") == "speech" and not is_tier1("MPC Member Mann Speaks")
    assert classify("Building Permits") == "other"


def test_tier1_events_are_upgraded_to_high_impact():
    e = EconomicEvent(datetime(2024, 3, 12, 12, 30), "usd", "CPI y/y", "medium", "x")
    assert e.impact == HIGH and e.currency == "USD" and e.time.tzinfo is not None
    assert EconomicEvent(NOW, "USD", "Building Permits", "medium", "x").impact == MEDIUM
    assert EconomicEvent(NOW, "USD", "x", "weird", "x").impact == LOW
    assert e.id == EconomicEvent(datetime(2024, 3, 12, 12, 30, tzinfo=UTC), "USD", "cpi y/y", "high", "y").id


def test_faireconomy_provider_parses_recorded_sample():
    p = FairEconomyProvider(fetch=lambda url, params: FF_SAMPLE)
    events = p.fetch(NOW - timedelta(days=2), NOW + timedelta(days=7))
    assert len(events) == 2                                     # holiday + broken row skipped
    cpi = next(e for e in events if e.currency == "USD")
    assert cpi.time == datetime(2024, 3, 12, 12, 30, tzinfo=UTC) and cpi.impact == HIGH
    assert cpi.forecast == "0.4%" and cpi.source == "faireconomy" and cpi.confirmed
    assert next(e for e in events if e.currency == "EUR").impact == HIGH    # CPI is tier-1


def test_providers_raise_instead_of_returning_empty_on_failure(tmp_path):
    def boom(url, params):
        raise ProviderError("HTTP 503")

    with pytest.raises(ProviderError):
        FairEconomyProvider(fetch=boom).fetch(NOW, NOW)
    with pytest.raises(ProviderError):
        FairEconomyProvider(fetch=lambda u, p: "<html>").fetch(NOW, NOW)
    with pytest.raises(ProviderError, match="FINNHUB_API_KEY"):
        FinnhubProvider(fetch=lambda u, p: "{}").fetch(NOW, NOW)
    with pytest.raises(ProviderError):
        FileProvider(tmp_path / "missing.csv").fetch(NOW, NOW)
    with pytest.raises(ValueError):
        build_providers(["made_up_source"])
    with pytest.raises(ValueError):
        build_providers(["file"])


def test_finnhub_and_file_providers(tmp_path):
    seen = {}

    def fetch(url, params):
        seen.update(params)
        return FINNHUB_SAMPLE

    ev = FinnhubProvider(fetch=fetch, api_key="k").fetch(datetime(2024, 3, 4, tzinfo=UTC),
                                                        datetime(2024, 3, 10, tzinfo=UTC))
    assert len(ev) == 1 and ev[0].currency == "USD" and ev[0].actual == "275" and seen["token"] == "k"
    f = tmp_path / "cal.csv"
    f.write_text("time,currency,title,impact\n2024-03-12T12:30:00Z,USD,CPI m/m,high\nbad,USD,x,high\n")
    got = FileProvider(f).fetch(NOW - timedelta(days=1), NOW + timedelta(days=1))
    assert len(got) == 1 and got[0].time.hour == 12 and got[0].source == "file"


def test_official_feeds_are_primary_sources_only():
    feeds = OfficialFeeds(fetch=lambda url, params: RSS_SAMPLE)
    heads = feeds.headlines(now=NOW)
    assert len(heads) == 2 and all(h["confirmed"] for h in heads)          # one fresh item per feed
    assert {h["source"] for h in heads} == {"federalreserve.gov", "ecb.europa.eu"}
    assert OfficialFeeds(fetch=lambda u, p: "not xml").headlines(now=NOW) == []


def monitor(db, events=(), **cfg) -> NewsMonitor:
    class Static:
        name = "static"

        def fetch(self, start, end):
            return list(events)

    m = NewsMonitor(NewsConfig(**cfg), db, [Static()])
    assert m.refresh(NOW, force=True)
    return m


CPI = EconomicEvent(datetime(2024, 3, 12, 12, 30, tzinfo=UTC), "USD", "CPI m/m", "high", "test")
MINOR = EconomicEvent(datetime(2024, 3, 12, 12, 30, tzinfo=UTC), "USD", "Building Permits", "medium", "test")


def test_entries_blocked_around_high_impact_event(db):
    m = monitor(db, [CPI, MINOR])
    usd = {"EUR", "USD"}
    far = m.state(usd, datetime(2024, 3, 12, 9, 0, tzinfo=UTC))
    assert not far.blocked and far.risk_factor == 1.0
    reduce = m.state(usd, datetime(2024, 3, 12, 11, 0, tzinfo=UTC))           # 90 min before
    assert not reduce.blocked and reduce.risk_factor == 0.5 and "reduced size" in reduce.notes[0]
    before = m.state(usd, datetime(2024, 3, 12, 12, 10, tzinfo=UTC))
    assert before.blocked and "CPI" in before.reason and before.risk_factor == 0.0
    after = m.state(usd, datetime(2024, 3, 12, 12, 55, tzinfo=UTC))
    assert after.blocked
    assert not m.state(usd, datetime(2024, 3, 12, 13, 5, tzinfo=UTC)).blocked
    # an unrelated pair is unaffected
    assert not m.state({"AUD", "NZD"}, datetime(2024, 3, 12, 12, 10, tzinfo=UTC)).blocked
    text = before.describe(datetime(2024, 3, 12, 12, 10, tzinfo=UTC))
    assert "USD CPI m/m in 20 min" in text and "confirmed schedule" in text and "source: test" in text


def test_medium_impact_does_not_block(db):
    m = monitor(db, [MINOR])
    assert not m.state({"USD"}, datetime(2024, 3, 12, 12, 25, tzinfo=UTC)).blocked
    assert len(m.upcoming(datetime(2024, 3, 12, 9, 0, tzinfo=UTC))) == 1


def test_fails_closed_when_calendar_is_unavailable(db):
    class Down:
        name = "down"

        def fetch(self, start, end):
            raise ProviderError("HTTP 503")

    m = NewsMonitor(NewsConfig(), db, [Down()])
    assert m.refresh(NOW, force=True) is False
    st = m.state({"USD"}, NOW)
    assert st.blocked and "fail-closed" in st.reason
    # data that was fetched goes stale after `stale_after_hours`
    ok = monitor(db)
    assert not ok.state({"USD"}, NOW + timedelta(hours=35)).blocked
    assert ok.state({"USD"}, NOW + timedelta(hours=37)).blocked
    relaxed = NewsMonitor(NewsConfig(fail_closed=False), db, [Down()])
    assert not relaxed.state({"USD"}, NOW + timedelta(days=30)).blocked
    assert not NewsMonitor(NewsConfig(enabled=False), db, []).state({"USD"}, NOW).blocked


def test_manual_blackout_and_pre_news_exit(db):
    b = Blackout(start="2024-03-12T10:00:00Z", end="2024-03-12T14:00:00Z", currencies=["GBP"], reason="election")
    m = monitor(db, manual_blackouts=[b])
    assert m.state({"GBP", "USD"}, NOW).blocked and "election" in m.state({"GBP", "USD"}, NOW).reason
    assert not m.state({"EUR", "USD"}, NOW).blocked
    closing = monitor(db, [CPI], close_before_tier1_minutes=15)
    assert closing.state({"USD"}, datetime(2024, 3, 12, 12, 20, tzinfo=UTC)).close_positions
    assert not closing.state({"USD"}, datetime(2024, 3, 12, 11, 20, tzinfo=UTC)).close_positions


def test_refresh_is_throttled(db):
    calls = []

    class Counting:
        name = "c"

        def fetch(self, start, end):
            calls.append(1)
            return []

    m = NewsMonitor(NewsConfig(), db, [Counting()])
    m.refresh(NOW)
    m.refresh(NOW + timedelta(minutes=10))
    m.refresh(NOW + timedelta(minutes=61))
    assert len(calls) == 2


def test_backtest_blackout_mask():
    idx = pd.date_range("2024-03-12 11:00", periods=13, freq="15min", tz="UTC")
    mask = blackout_mask(idx, [CPI, MINOR], {"EUR", "USD"}, 30, 30)
    assert list(idx[mask].strftime("%H:%M")) == ["12:00", "12:15", "12:30", "12:45", "13:00"]
    assert not blackout_mask(idx, [CPI], {"AUD", "NZD"}, 30, 30).any()


# ------------------------------------------------------------- notifications
class Capture(Channel):
    name = "capture"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, level, title, body):
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append((level, title, body))


def test_notifier_delivers_logs_and_redacts(db, monkeypatch):
    monkeypatch.setenv("MT5_PASSWORD", "TopSecretPw1")
    cap = Capture()
    n = Notifier([cap], db=db)
    assert n.notify("warning", "Risk", "password=TopSecretPw1 leaked TopSecretPw1")
    assert "TopSecretPw1" not in cap.sent[0][2]
    row = db.recent_notifications()[0]
    assert row["delivered"] == "capture" and "TopSecretPw1" not in row["body"]


def test_notifier_rate_limits_but_never_drops_critical(db):
    t = [1000.0]
    cap = Capture()
    n = Notifier([cap], db=db, min_interval_seconds=300, clock=lambda: t[0])
    assert n.notify("warning", "Spread wide", key="spread")
    assert not n.notify("warning", "Spread wide", key="spread")
    t[0] += 301
    assert n.notify("warning", "Spread wide", key="spread")
    assert n.notify("critical", "STOP", key="k") and n.notify("critical", "STOP", key="k")
    assert len(cap.sent) == 4


def test_broken_channel_never_raises(db):
    good = Capture()
    n = Notifier([Capture(fail=True), good], db=db)
    assert n.notify("critical", "x", "y") and len(good.sent) == 1


def test_telegram_and_webhook_payloads():
    calls = []

    class R:
        status_code = 200

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        return R()

    TelegramChannel("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", "42", post=post).send("info", "T", "B")
    WebhookChannel("https://hooks.example/x", post=post).send("critical", "T", "B")
    assert calls[0][0].endswith("/sendMessage") and calls[0][1]["chat_id"] == "42"
    assert "[CRITICAL] T" in calls[1][1]["text"] and "content" not in calls[1][1]
    WebhookChannel("https://discord.com/api/webhooks/123/abc", post=post).send("warning", "T", "x" * 5000)
    discord = calls[2][1]
    assert set(discord) == {"content", "username"} and discord["content"].startswith("[WARNING] T")
    assert len(discord["content"]) <= 2000                         # Discord's message limit


def test_notifier_from_env_has_no_channels_by_default(db):
    assert Notifier.from_env(db).channels == []
