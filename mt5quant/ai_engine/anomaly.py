"""Detection of abnormal market conditions (unusual volatility, spread
blow-outs, price gaps, stale quotes). Flags only ever *restrict* trading."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from ..core.models import SymbolSpec, Tick
from ..indicators import ta
from .regime import robust_z


@dataclass
class AnomalyReport:
    flags: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.flags)


def volatility_shock(df: pd.DataFrame, n: int = 100) -> pd.Series:
    return robust_z(ta.true_range(df), n)


def check_market(df: pd.DataFrame, tick: Optional[Tick], spec: SymbolSpec, now: datetime,
                 typical_spread_points: Optional[float] = None, shock_z: float = 6.0,
                 spread_mult: float = 3.0, gap_atr: float = 3.0, max_tick_age: float = 120.0) -> AnomalyReport:
    """Inspect the latest completed bars and the live quote."""
    rep = AnomalyReport()
    if len(df) >= 60:
        z = volatility_shock(df).iloc[-3:].max()
        rep.details["volatility_z"] = None if np.isnan(z) else round(float(z), 2)
        if z == z and z >= shock_z:
            rep.flags.append(f"volatility shock (range {z:.1f} robust-sigma above normal)")
        a = ta.atr(df, 14).iloc[-1]
        gap = abs(df["open"].iloc[-1] - df["close"].iloc[-2])
        if a == a and a > 0 and gap >= gap_atr * a:
            rep.flags.append(f"price gap of {gap / a:.1f} ATR")
    if tick is not None:
        age = (now - tick.time).total_seconds()
        rep.details["tick_age_s"] = round(age, 1)
        if age > max_tick_age:
            rep.flags.append(f"stale quote ({age:.0f}s old)")
        if tick.ask <= tick.bid:
            rep.flags.append("crossed or zero spread quote")
        spread_pts = tick.spread / spec.point
        rep.details["spread_points"] = round(spread_pts, 1)
        ref = typical_spread_points
        if "spread" in df.columns and len(df) >= 50:
            med = float(df["spread"].iloc[-200:].median())
            ref = max(med, ref or 0.0) if med > 0 else ref
        if ref and spread_pts > spread_mult * ref:
            rep.flags.append(f"spread {spread_pts:.0f} pts is {spread_pts / ref:.1f}x normal")
    return rep
