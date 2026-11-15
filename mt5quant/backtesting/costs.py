"""Transaction-cost model shared by the backtester and the paper broker.

All assumptions lean conservative: slippage is always adverse, execution
delay is always adverse, take-profits never get positive slippage, and when a
stop and a target fall inside the same bar the stop is assumed to be hit first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class CostModel:
    spread_points: dict[str, float] = field(default_factory=dict)
    default_spread_points: float = 12.0
    commission_per_lot_per_side: float = 3.5
    slippage_points_mean: float = 2.0
    execution_delay_ms: int = 250
    swap_long_points: float = -5.0          # per night, per lot; conservative placeholder
    swap_short_points: float = -5.0
    spread_multiplier: float = 1.0          # stress-test knob
    slippage_multiplier: float = 1.0        # stress-test knob
    seed: int = 12345

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    @classmethod
    def from_config(cls, cfg, **overrides) -> "CostModel":
        base = dict(spread_points=dict(cfg.spread_points), default_spread_points=cfg.default_spread_points,
                    commission_per_lot_per_side=cfg.commission_per_lot_per_side,
                    slippage_points_mean=cfg.slippage_points_mean, execution_delay_ms=cfg.execution_delay_ms)
        base.update(overrides)
        return cls(**base)

    def stressed(self, spread_mult: float = 1.0, slippage_mult: float = 1.0) -> "CostModel":
        return CostModel(spread_points=dict(self.spread_points), default_spread_points=self.default_spread_points,
                         commission_per_lot_per_side=self.commission_per_lot_per_side,
                         slippage_points_mean=self.slippage_points_mean,
                         execution_delay_ms=self.execution_delay_ms, swap_long_points=self.swap_long_points,
                         swap_short_points=self.swap_short_points,
                         spread_multiplier=self.spread_multiplier * spread_mult,
                         slippage_multiplier=self.slippage_multiplier * slippage_mult, seed=self.seed)

    def reseed(self, seed: int) -> None:
        self.seed = seed
        self.rng = np.random.default_rng(seed)

    def typical_spread_points(self, symbol: str) -> float:
        return self.spread_points.get(symbol, self.default_spread_points) * self.spread_multiplier

    def spread_price(self, symbol: str, point: float, bar_spread_points: float | None = None) -> float:
        """Spread in price units. Uses the recorded per-bar spread when the
        dataset has one (never less than the configured typical spread's half,
        because recorded bar spreads are often the *minimum* of the bar)."""
        typical = self.spread_points.get(symbol, self.default_spread_points)
        pts = typical if bar_spread_points is None or not np.isfinite(bar_spread_points) \
            else max(float(bar_spread_points), 0.5 * typical)
        return pts * self.spread_multiplier * point

    def slippage_price(self, point: float, bar_range: float = 0.0, bar_seconds: float = 60.0) -> float:
        """Adverse slippage for a market/stop fill, in price units."""
        base = self.rng.exponential(self.slippage_points_mean) * point if self.slippage_points_mean > 0 else 0.0
        delay = 0.0
        if self.execution_delay_ms > 0 and bar_range > 0 and bar_seconds > 0:
            sigma_per_sqrt_s = (bar_range / 1.67) / np.sqrt(bar_seconds)
            delay = 0.5 * abs(self.rng.standard_normal()) * sigma_per_sqrt_s * \
                np.sqrt(self.execution_delay_ms / 1000.0)
        return float((base + delay) * self.slippage_multiplier)

    def commission(self, volume: float) -> float:
        return self.commission_per_lot_per_side * volume
