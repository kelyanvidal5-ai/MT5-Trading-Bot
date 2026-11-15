"""Risk limits with hard-coded safety ceilings.

Every limit is configurable, but only *inside* the envelope defined by
``HARD_CEILINGS``. A config file that omits a limit, sets it to zero/null, or
pushes it past its ceiling is rejected and the engine refuses to start. That
is what "configurable but protected against accidental removal" means here:
you can tighten freely, loosen only up to the ceiling, and never delete.

Raising a ceiling requires editing this file, i.e. a reviewed code change -
it cannot happen through configuration, the dashboard or the AI engine.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Absolute maximums. Config values above these are rejected, not clamped,
# so a typo ("25" instead of "0.25") fails loudly instead of silently trading.
HARD_CEILINGS: dict[str, float] = {
    "risk_per_trade_pct": 1.0,
    "max_daily_loss_pct": 3.0,
    "max_weekly_loss_pct": 6.0,
    "max_drawdown_pct": 10.0,
    "max_open_positions": 10,
    "max_total_open_risk_pct": 3.0,
    "max_currency_exposure_risk_pct": 2.0,
    "max_trades_per_day": 40,
    "max_slippage_points": 100,
    "max_spread_atr_ratio": 0.5,
}

# Minimums for "floor" style limits (lower is more dangerous).
HARD_FLOORS: dict[str, float] = {
    "min_margin_level_pct": 150.0,
    "min_reward_risk": 0.5,
    "max_margin_usage_pct": 1.0,   # must be >= 1 to be meaningful
}


class RiskLimits(BaseModel):
    """Validated risk envelope. Defaults are the conservative initial config."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_per_trade_pct: float = Field(0.25, gt=0)
    max_daily_loss_pct: float = Field(1.0, gt=0)
    max_weekly_loss_pct: float = Field(2.5, gt=0)
    max_drawdown_pct: float = Field(5.0, gt=0)
    max_open_positions: int = Field(3, gt=0)
    max_positions_per_symbol: int = Field(1, gt=0, le=2)
    max_total_open_risk_pct: float = Field(0.75, gt=0)
    # Net risk allowed on any single currency across all open positions
    # (EURUSD long + EURJPY long are both long EUR -> correlated).
    max_currency_exposure_risk_pct: float = Field(0.5, gt=0)
    max_trades_per_day: int = Field(10, gt=0)
    max_consecutive_losses: int = Field(4, gt=0, le=10)   # cool-down trigger
    cooldown_minutes_after_loss_streak: int = Field(240, ge=30)
    max_spread_atr_ratio: float = Field(0.15, gt=0)       # spread / ATR(entry tf)
    max_slippage_points: int = Field(20, gt=0)
    min_margin_level_pct: float = Field(300.0, gt=0)
    max_margin_usage_pct: float = Field(20.0, gt=0, le=50)
    min_reward_risk: float = Field(1.0, gt=0)
    max_order_errors_per_hour: int = Field(5, gt=0, le=20)  # abnormal-behaviour breaker
    flatten_on_emergency: bool = True

    # Structural prohibitions. They exist as fields only so that a config
    # trying to enable them fails validation with a clear message.
    allow_martingale: bool = False
    allow_grid: bool = False
    allow_averaging_down: bool = False

    @model_validator(mode="after")
    def _enforce_envelope(self) -> "RiskLimits":
        for name, ceiling in HARD_CEILINGS.items():
            value = getattr(self, name)
            if value > ceiling:
                raise ValueError(
                    f"risk limit '{name}'={value} exceeds the hard ceiling {ceiling}. "
                    "Ceilings can only be changed in code (risk_management/limits.py)."
                )
        for name, floor in HARD_FLOORS.items():
            value = getattr(self, name)
            if value < floor:
                raise ValueError(f"risk limit '{name}'={value} is below the hard floor {floor}.")
        if self.allow_martingale or self.allow_grid or self.allow_averaging_down:
            raise ValueError(
                "martingale, grid and averaging-down are structurally prohibited; "
                "they cannot be enabled through configuration."
            )
        if self.max_daily_loss_pct < self.risk_per_trade_pct:
            raise ValueError("max_daily_loss_pct must be >= risk_per_trade_pct")
        if self.max_weekly_loss_pct < self.max_daily_loss_pct:
            raise ValueError("max_weekly_loss_pct must be >= max_daily_loss_pct")
        if self.max_drawdown_pct < self.max_daily_loss_pct:
            raise ValueError("max_drawdown_pct must be >= max_daily_loss_pct")
        return self
