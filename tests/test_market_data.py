from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from mt5quant.market_data.loader import load_bars, quality_report, save_bars
from mt5quant.market_data.sessions import MarketCalendar
from mt5quant.market_data.specs import generic_spec
from mt5quant.market_data.synthetic import generate_bars
from mt5quant.market_data.timeframes import MarketData, align_htf, drop_incomplete, resample, validate_bars

UTC = timezone.utc


def test_synthetic_is_deterministic_and_valid(bars_m5):
    again = generate_bars("EURUSD", periods=50_000, tf="M5", seed=11)
    pd.testing.assert_frame_equal(bars_m5, again)
    validate_bars(bars_m5)
    assert bars_m5.attrs["synthetic"] is True
    ny = bars_m5.index.tz_convert("America/New_York")
    assert not (ny.weekday == 5).any()                     # no Saturday bars


def test_resample_matches_manual_aggregation(bars_m5):
    h1 = resample(bars_m5, "H1")
    ts = h1.index[100]
    chunk = bars_m5[(bars_m5.index >= ts) & (bars_m5.index < ts + pd.Timedelta(hours=1))]
    assert h1.loc[ts, "open"] == chunk["open"].iloc[0]
    assert h1.loc[ts, "high"] == chunk["high"].max()
    assert h1.loc[ts, "low"] == chunk["low"].min()
    assert h1.loc[ts, "close"] == chunk["close"].iloc[-1]


def test_daily_candles_five_per_week(bars_m5):
    d1 = resample(bars_m5, "D1")
    per_week = d1.groupby(d1.index.tz_convert("America/New_York").strftime("%G-%V")).size()
    assert per_week.iloc[1:-1].max() <= 6 and per_week.iloc[1:-1].median() == 5


def test_align_htf_never_exposes_unfinished_bar(bars_m5):
    h1 = resample(bars_m5, "H1")
    aligned = align_htf(bars_m5.index, "M5", h1["close"], "H1")
    # The M5 bar opening at hh:50 closes at hh:55 -> the H1 bar hh:00 is NOT finished yet.
    i = np.flatnonzero((bars_m5.index.minute == 50))[500]
    ts = bars_m5.index[i]
    prev_hour = ts.floor("h") - pd.Timedelta(hours=1)
    if prev_hour in h1.index:
        assert aligned.iloc[i] == h1.loc[prev_hour, "close"]
    # The M5 bar opening at hh:55 closes exactly at the hour -> the H1 bar is known.
    j = i + 1
    if bars_m5.index[j].minute == 55 and bars_m5.index[j].floor("h") in h1.index:
        assert aligned.iloc[j] == h1.loc[bars_m5.index[j].floor("h"), "close"]


def test_align_htf_is_invariant_to_future_data(bars_m5):
    """Truncating the future must not change any already-known aligned value."""
    cut = 30_000
    full = align_htf(bars_m5.index, "M5", resample(bars_m5, "H4")["close"], "H4").iloc[:cut]
    part_df = bars_m5.iloc[:cut]
    part = align_htf(part_df.index, "M5", resample(part_df, "H4")["close"], "H4")
    pd.testing.assert_series_equal(full, part)


def test_drop_incomplete():
    idx = pd.date_range("2024-01-02 10:00", periods=3, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx)
    out = drop_incomplete(df, "H1", pd.Timestamp("2024-01-02 12:30", tz="UTC"))
    assert list(out.index) == list(idx[:2])


def test_validate_bars_rejects_bad_data():
    idx = pd.date_range("2024-01-02", periods=3, freq="5min", tz="UTC")
    bad = pd.DataFrame({"open": [1, 1, 1], "high": [1, 0.5, 1], "low": [1, 1, 1], "close": [1, 1, 1]}, index=idx)
    with pytest.raises(ValueError):
        validate_bars(bad)
    with pytest.raises(ValueError):
        validate_bars(pd.DataFrame({"open": [1.0]}, index=idx[:1]))


def test_loader_roundtrip_and_mt5_export(tmp_path, bars_m5):
    p = tmp_path / "eurusd.csv"
    save_bars(bars_m5.iloc[:500], p)
    back = load_bars(p)
    assert len(back) == 500 and back["close"].iloc[-1] == bars_m5["close"].iloc[499]
    mt5 = tmp_path / "export.csv"
    mt5.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                   "2024.01.02\t10:00:00\t1.1\t1.2\t1.0\t1.15\t100\t0\t8\n"
                   "2024.01.02\t10:05:00\t1.15\t1.25\t1.1\t1.2\t120\t0\t9\n")
    df = load_bars(mt5)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "spread"]
    assert df.index[0] == pd.Timestamp("2024-01-02 10:00", tz="UTC")
    rep = quality_report(bars_m5, "M5")
    assert rep["synthetic"] is True and rep["bars"] == 50_000


# ------------------------------------------------------------------ sessions
CAL = MarketCalendar()


@pytest.mark.parametrize("when,expected", [
    (datetime(2024, 3, 9, 12, 0, tzinfo=UTC), False),     # Saturday
    (datetime(2024, 3, 10, 20, 0, tzinfo=UTC), False),    # Sunday before 17:00 NY (EDT started: 21:00 UTC)
    (datetime(2024, 3, 10, 21, 30, tzinfo=UTC), True),    # Sunday after open
    (datetime(2024, 3, 13, 12, 0, tzinfo=UTC), True),     # Wednesday
    (datetime(2024, 3, 15, 20, 30, tzinfo=UTC), True),    # Friday before close
    (datetime(2024, 3, 15, 21, 30, tzinfo=UTC), False),   # Friday after 17:00 NY
    (datetime(2024, 1, 12, 21, 30, tzinfo=UTC), True),    # winter: close is 22:00 UTC
    (datetime(2024, 1, 12, 22, 30, tzinfo=UTC), False),
    (datetime(2024, 12, 25, 12, 0, tzinfo=UTC), False),   # Christmas
    (datetime(2025, 1, 1, 12, 0, tzinfo=UTC), False),     # New Year
])
def test_fx_week_and_holidays(when, expected):
    assert CAL.is_open(when) is expected


def test_crypto_always_open():
    assert CAL.is_open(datetime(2024, 3, 9, 12, 0, tzinfo=UTC), "crypto")


def test_entry_rules():
    ok, _ = CAL.entry_allowed(datetime(2024, 3, 13, 12, 0, tzinfo=UTC))
    assert ok
    assert CAL.entry_allowed(datetime(2024, 3, 13, 21, 10, tzinfo=UTC))[0] is False     # rollover
    assert CAL.entry_allowed(datetime(2024, 3, 15, 19, 30, tzinfo=UTC))[0] is False     # Friday cutoff
    assert CAL.entry_allowed(datetime(2024, 3, 10, 21, 30, tzinfo=UTC))[0] is False     # weekly open
    assert CAL.entry_allowed(datetime(2024, 12, 24, 12, 0, tzinfo=UTC))[0] is False     # thin holiday
    assert MarketCalendar(holidays=["2024-07-04"]).is_open(datetime(2024, 7, 4, 12, tzinfo=UTC)) is False


def test_sessions():
    assert set(CAL.active_sessions(datetime(2024, 3, 13, 13, 30, tzinfo=UTC))) == {"london", "newyork"}
    assert "tokyo" in CAL.active_sessions(datetime(2024, 3, 13, 2, 0, tzinfo=UTC))


def test_vectorized_entry_mask_matches_scalar(bars_m5):
    idx = bars_m5.index[::37][:1500]
    mask = CAL.entry_mask(idx)
    scalar = np.array([CAL.entry_allowed(t.to_pydatetime())[0] for t in idx])
    assert (mask == scalar).all()


def test_market_data_container(bars_m5):
    md = MarketData.from_base("EURUSD", generic_spec("EURUSD"), bars_m5, "M5")
    assert {"M5", "M15", "H1", "H4", "D1", "W1"} <= set(md.frames)
    assert "M1" not in md.frames
    aligned = md.htf(md.frame("H1")["close"], "H1", "M15")
    assert len(aligned) == len(md.frame("M15"))
    with pytest.raises(KeyError):
        md.frame("M1")
