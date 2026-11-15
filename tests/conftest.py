from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mt5quant.core.config import AppConfig, load_config  # noqa: E402
from mt5quant.database.db import Database  # noqa: E402
from mt5quant.market_data.specs import generic_spec  # noqa: E402
from mt5quant.market_data.synthetic import generate_bars  # noqa: E402
from mt5quant.market_data.timeframes import MarketData  # noqa: E402


@pytest.fixture(scope="session")
def bars_m5() -> pd.DataFrame:
    """~8 months of synthetic EURUSD M5 bars (deterministic)."""
    return generate_bars("EURUSD", periods=50_000, tf="M5", seed=11)


@pytest.fixture(scope="session")
def market_data(bars_m5) -> MarketData:
    return MarketData.from_base("EURUSD", generic_spec("EURUSD"), bars_m5, "M5")


@pytest.fixture()
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture()
def config() -> AppConfig:
    return load_config(use_local=False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MT5QUANT_LIVE_AUTHORIZED", "DASHBOARD_TOKEN", "MT5_BRIDGE_SECRET", "MT5_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
