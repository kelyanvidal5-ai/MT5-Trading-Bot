"""Configuration loading and validation.

Non-secret settings live in YAML (``config/default.yaml`` overlaid by an
optional git-ignored ``config/local.yaml``). Secrets are read from environment
variables only (optionally via a git-ignored ``.env``) and are never written
back to disk or to logs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..risk_management.limits import RiskLimits
from .models import TradingMode

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"
LOCAL_CONFIG = ROOT / "config" / "local.yaml"

LIVE_AUTH_PHRASE = "I_AUTHORIZE_REAL_MONEY_TRADING"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaperConfig(_Model):
    initial_balance: float = Field(10_000.0, gt=0)
    currency: str = "USD"
    leverage: int = Field(30, gt=0, le=500)
    # synthetic: built-in random-walk feed (for demonstration/tests only)
    # mt5: quotes from a local terminal (Windows) ; bridge: quotes via the gateway
    data_source: str = "synthetic"
    synthetic_seed: int = 7
    synthetic_speed: float = Field(1.0, gt=0)   # 1 synthetic M1 bar per (60/speed) seconds

    @field_validator("data_source")
    @classmethod
    def _ds(cls, v: str) -> str:
        if v not in {"synthetic", "mt5", "bridge"}:
            raise ValueError("paper.data_source must be synthetic | mt5 | bridge")
        return v


class StrategyConfig(_Model):
    enabled: list[str] = Field(default_factory=list)
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    min_confidence: float = Field(0.55, ge=0.3, le=0.95)
    # In DEMO/LIVE only strategies with a VALIDATED status may trade. This flag
    # only relaxes the rule for PAPER mode.
    allow_unvalidated_in_paper: bool = True
    use_meta_model: bool = True


class NewsConfig(_Model):
    enabled: bool = True
    providers: list[str] = Field(default_factory=lambda: ["faireconomy"])
    calendar_file: Optional[str] = None           # for the "file" provider
    refresh_minutes: int = Field(60, ge=5)
    block_minutes_before: int = Field(30, ge=5)
    block_minutes_after: int = Field(30, ge=5)
    reduce_risk_minutes_before: int = Field(120, ge=0)
    reduce_risk_factor: float = Field(0.5, gt=0, le=1.0)
    close_before_tier1_minutes: int = Field(0, ge=0)   # 0 = keep positions through events
    # If the calendar cannot be fetched for this long, new entries are suspended.
    stale_after_hours: int = Field(36, ge=1)
    fail_closed: bool = True


class SessionConfig(_Model):
    avoid_rollover_minutes: int = Field(65, ge=0)      # around 17:00 New York
    friday_cutoff_hours_before_close: float = Field(2.0, ge=0)
    monday_open_delay_minutes: int = Field(60, ge=0)
    holidays: list[str] = Field(default_factory=list)  # extra YYYY-MM-DD dates (no trading)
    max_tick_age_seconds: int = Field(120, ge=5)


class ExecutionConfig(_Model):
    magic: int = Field(550100, gt=0)
    max_retries: int = Field(2, ge=0, le=5)
    retry_delay_seconds: float = Field(0.5, ge=0)
    pending_expiry_bars: int = Field(3, ge=1)


class EngineConfig(_Model):
    loop_interval_seconds: float = Field(5.0, gt=0)
    heartbeat_seconds: float = Field(30.0, gt=0)
    history_bars: int = Field(1500, ge=600)
    equity_snapshot_seconds: float = Field(60.0, gt=0)
    reconnect_max_backoff_seconds: float = Field(300.0, gt=0)
    daily_report_hour_utc: int = Field(22, ge=0, le=23)
    day_reset_hour_utc: int = Field(0, ge=0, le=23)


class DashboardConfig(_Model):
    host: str = "127.0.0.1"
    port: int = Field(8050, gt=0, lt=65536)


class BridgeConfig(_Model):
    host: str = "127.0.0.1"
    port: int = Field(8787, gt=0, lt=65536)
    max_clock_skew_seconds: int = Field(30, ge=5, le=300)
    timeout_seconds: float = Field(10.0, gt=0)


class CostConfig(_Model):
    """Backtest/paper cost assumptions. Override per symbol with broker data."""

    spread_points: dict[str, float] = Field(default_factory=dict)   # typical spread per symbol
    default_spread_points: float = Field(12.0, ge=0)
    commission_per_lot_per_side: float = Field(3.5, ge=0)          # account currency
    slippage_points_mean: float = Field(2.0, ge=0)
    execution_delay_ms: int = Field(250, ge=0)
    rollover_spread_multiplier: float = Field(4.0, ge=1)
    news_spread_multiplier: float = Field(3.0, ge=1)


class DemoGateConfig(_Model):
    """Evidence required from DEMO trading before LIVE can be authorized."""

    min_demo_days: int = Field(30, ge=20)
    min_demo_trades: int = Field(50, ge=30)
    authorization_valid_days: int = Field(90, ge=1, le=365)


class AppConfig(_Model):
    mode: TradingMode = TradingMode.PAPER
    symbols: list[str] = Field(default_factory=list)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    strategies: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    news: NewsConfig = Field(default_factory=NewsConfig)
    sessions: SessionConfig = Field(default_factory=SessionConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    demo_gate: DemoGateConfig = Field(default_factory=DemoGateConfig)
    broker_backend: str = "mt5"            # for DEMO/LIVE: mt5 (direct, Windows) | bridge
    database_path: str = "data/mt5quant.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate symbols in config")
        return cleaned

    @field_validator("broker_backend")
    @classmethod
    def _backend(cls, v: str) -> str:
        if v not in {"mt5", "bridge"}:
            raise ValueError("broker_backend must be mt5 | bridge")
        return v

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str | Path] = None, overrides: Optional[dict] = None,
                use_local: bool = True) -> AppConfig:
    """Load YAML config -> validated AppConfig. Raises on any invalid value."""
    data: dict = {}
    primary = Path(path) if path else DEFAULT_CONFIG
    if primary.exists():
        data = yaml.safe_load(primary.read_text()) or {}
    elif path:
        raise FileNotFoundError(f"config file not found: {primary}")
    if use_local and not path and LOCAL_CONFIG.exists():
        data = _deep_merge(data, yaml.safe_load(LOCAL_CONFIG.read_text()) or {})
    if overrides:
        data = _deep_merge(data, overrides)
    return AppConfig.model_validate(data)


class Secrets:
    """Access to secrets held in environment variables.

    Values are fetched on demand and never cached in config objects, so they
    cannot leak through ``repr(config)`` or a config dump.
    """

    _loaded = False

    @classmethod
    def _ensure(cls) -> None:
        if cls._loaded:
            return
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
        except Exception:  # pragma: no cover - dotenv is optional
            pass
        cls._loaded = True

    @classmethod
    def get(cls, name: str, default: str = "") -> str:
        cls._ensure()
        return os.environ.get(name, default) or default

    @classmethod
    def require(cls, name: str) -> str:
        value = cls.get(name)
        if not value:
            raise RuntimeError(f"required secret '{name}' is not set (see .env.example)")
        return value

    @classmethod
    def all_values(cls) -> list[str]:
        """Secret values currently set - used by the log redaction filter."""
        cls._ensure()
        names = [
            "MT5_PASSWORD", "MT5_BRIDGE_SECRET", "DASHBOARD_TOKEN", "TELEGRAM_BOT_TOKEN",
            "SMTP_PASSWORD", "FINNHUB_API_KEY", "TRADING_ECONOMICS_API_KEY", "NOTIFY_WEBHOOK_URL",
        ]
        return [v for n in names if (v := os.environ.get(n)) and len(v) >= 6]
