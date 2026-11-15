"""Position sizing: fixed-fractional risk from the stop distance.

volume = (equity x risk%) / (loss per lot if the stop is hit)

The result is always rounded DOWN to the broker's volume step. If even the
minimum lot would risk more than allowed, the trade is refused rather than
rounded up. Size never depends on previous wins or losses (no martingale).
"""
from __future__ import annotations

import math

from ..core.models import SymbolSpec


def loss_per_lot(entry: float, stop: float, spec: SymbolSpec) -> float:
    """Account-currency loss of 1.0 lot if price goes from entry to stop."""
    if spec.tick_size <= 0 or spec.tick_value <= 0:
        return float("inf")
    return abs(entry - stop) / spec.tick_size * spec.tick_value


def floor_to_step(volume: float, spec: SymbolSpec) -> float:
    if spec.volume_step <= 0:
        return 0.0
    steps = math.floor(volume / spec.volume_step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(spec.volume_step) + 1e-9)))
    return round(steps * spec.volume_step, decimals)


def position_size(equity: float, risk_pct: float, entry: float, stop: float, spec: SymbolSpec) -> tuple[float, float]:
    """Returns (volume, risk_amount). volume is 0.0 when the trade cannot be
    sized within the risk budget."""
    per_lot = loss_per_lot(entry, stop, spec)
    if equity <= 0 or risk_pct <= 0 or not math.isfinite(per_lot) or per_lot <= 0:
        return 0.0, 0.0
    budget = equity * risk_pct / 100.0
    volume = min(floor_to_step(budget / per_lot, spec), spec.volume_max)
    if volume < spec.volume_min:
        return 0.0, 0.0
    return volume, volume * per_lot
