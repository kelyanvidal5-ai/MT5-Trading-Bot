"""Management of open positions: break-even, trailing stop, time stop.

One pure function (`plan`) decides; the backtester and the live engine both
call it. Stops only ever move in the position's favour - a stop is never
widened and never removed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.models import ManagementPlan


@dataclass
class ManageAction:
    new_sl: Optional[float] = None
    close: bool = False
    reason: str = ""


def plan(side_sign: int, entry: float, sl_current: float, sl_initial: float, price: float,
         mgmt: ManagementPlan, bars_held: int, point: float = 0.0, min_step: float = 0.0) -> ManageAction:
    """``price`` is the latest close-side price (bid for longs, ask for
    shorts). ``sl_initial`` defines 1R."""
    if mgmt.max_bars is not None and bars_held >= mgmt.max_bars:
        return ManageAction(close=True, reason="time_exit")
    r = abs(entry - sl_initial)
    if r <= 0:
        return ManageAction()
    progress = (price - entry) * side_sign / r
    candidate = sl_current
    reason = ""
    if mgmt.breakeven_at_r is not None and progress >= mgmt.breakeven_at_r:
        be = entry + side_sign * 2 * point                 # entry plus a hair to cover costs
        if (be - candidate) * side_sign > 0:
            candidate, reason = be, "breakeven"
    if mgmt.trail_atr_mult is not None and mgmt.atr > 0 and progress >= mgmt.trail_start_r:
        trail = price - side_sign * mgmt.trail_atr_mult * mgmt.atr
        if (trail - candidate) * side_sign > 0:
            candidate, reason = trail, "trailing"
    # Only tighten, and only by a meaningful amount (avoids order spam).
    if (candidate - sl_current) * side_sign > max(min_step, point):
        # Never place the stop through the current price.
        if (price - candidate) * side_sign > 0:
            return ManageAction(new_sl=candidate, reason=reason)
    return ManageAction()
