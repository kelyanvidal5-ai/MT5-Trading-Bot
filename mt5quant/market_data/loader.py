"""Historical data loading and quality checks.

Supported inputs:
* MetaTrader 5 "Export bars" files (tab separated ``<DATE> <TIME> <OPEN> ...``)
* generic CSV with a time column and open/high/low/close[/volume/spread]
* Parquet/pickle written by ``scripts/download_history.py``
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..core.models import Timeframe
from .timeframes import validate_bars


def load_bars(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.suffix == ".parquet":
        return validate_bars(pd.read_parquet(p))
    if p.suffix in (".pkl", ".pickle"):
        return validate_bars(pd.read_pickle(p))
    head = p.read_text(errors="replace")[:400]
    if "<DATE>" in head.upper():
        return _load_mt5_export(p)
    return _load_generic_csv(p)


def _load_mt5_export(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p, sep="\t")
    df.columns = [c.strip("<>").lower() for c in df.columns]
    ts = df["date"].astype(str) + " " + (df["time"].astype(str) if "time" in df.columns else "00:00:00")
    df.index = pd.DatetimeIndex(pd.to_datetime(ts, format="mixed").values, tz="UTC")
    out = df.rename(columns={"tickvol": "volume"})
    keep = [c for c in ("open", "high", "low", "close", "volume", "spread") if c in out.columns]
    return validate_bars(out[keep])


def _load_generic_csv(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p)
    df.columns = [c.strip().lower() for c in df.columns]
    time_col = next((c for c in ("time", "datetime", "timestamp", "date") if c in df.columns), None)
    if time_col is None:
        raise ValueError(f"{p}: no time column found (expected one of time/datetime/timestamp/date)")
    col = df[time_col]
    if pd.api.types.is_numeric_dtype(col):
        unit = "ms" if col.iloc[0] > 1e11 else "s"
        idx = pd.to_datetime(col, unit=unit, utc=True)
    else:
        idx = pd.to_datetime(col, utc=True)
    df.index = pd.DatetimeIndex(idx)
    if "tick_volume" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"tick_volume": "volume"})
    return validate_bars(df.drop(columns=[time_col]))


def save_bars(df: pd.DataFrame, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index.name = "time"
    out.to_csv(p)


def quality_report(df: pd.DataFrame, tf: str) -> dict:
    """Describe the dataset so a backtest report can state what it was run on."""
    step = pd.Timedelta(minutes=Timeframe(tf).minutes)
    gaps = df.index.to_series().diff().dropna()
    intraweek = gaps[(gaps > step) & (gaps < pd.Timedelta(hours=40))]
    rng = (df["high"] - df["low"])
    return {
        "bars": int(len(df)),
        "start": str(df.index[0]) if len(df) else None,
        "end": str(df.index[-1]) if len(df) else None,
        "timeframe": tf,
        "synthetic": bool(df.attrs.get("synthetic", False)),
        "missing_bar_gaps": int(len(intraweek)),
        "largest_intraweek_gap_minutes": float(intraweek.max() / pd.Timedelta(minutes=1)) if len(intraweek) else 0.0,
        "zero_range_bars_pct": float((rng <= 0).mean() * 100) if len(df) else 0.0,
        "has_spread_column": "spread" in df.columns,
    }
