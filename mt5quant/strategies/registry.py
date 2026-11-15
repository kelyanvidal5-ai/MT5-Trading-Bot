"""Strategy registry."""
from __future__ import annotations

from typing import Any, Optional

from .base import Strategy
from .breakout import DonchianBreakout, VolSqueeze
from .reversion import BollingerReversion, LiquiditySweep
from .smc import SmcOrderBlock
from .trend import IntradayVwap, MacdMomentum, ScalpPullback, TrendEma

STRATEGIES: dict[str, type[Strategy]] = {cls.name: cls for cls in (
    ScalpPullback, IntradayVwap, TrendEma, DonchianBreakout, BollingerReversion, MacdMomentum,
    LiquiditySweep, SmcOrderBlock, VolSqueeze)}


def create(name: str, params: Optional[dict[str, Any]] = None) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"unknown strategy '{name}'. Available: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**(params or {}))


def create_all(names: list[str], params: Optional[dict[str, dict]] = None) -> list[Strategy]:
    params = params or {}
    return [create(n, params.get(n)) for n in names]
