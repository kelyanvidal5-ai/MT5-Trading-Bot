"""Wiring: build brokers, news monitor and engine from configuration."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..backtesting.costs import CostModel
from ..execution.bridge_client import BridgeBroker
from ..execution.broker import Broker, MarketDataSource
from ..execution.live_gate import LiveGate
from ..execution.paper import PaperBroker
from ..market_data.feeds import SyntheticFeed
from ..news_engine.monitor import NewsMonitor
from ..news_engine.providers import build_providers
from ..notifications.notifier import Notifier
from .config import ROOT, AppConfig, Secrets
from .engine import TradingEngine
from .models import TradingMode

log = logging.getLogger(__name__)

MODELS_DIR = ROOT / "models"


def _mt5_broker(config: AppConfig, db, read_only: bool) -> Broker:
    from ..execution.mt5_broker import MT5Broker

    return MT5Broker(mode=config.mode, gate=LiveGate(config, db), magic=config.execution.magic,
                     read_only=read_only)


def _bridge_broker(config: AppConfig) -> Broker:
    return BridgeBroker(Secrets.get("MT5_BRIDGE_URL", f"http://{config.bridge.host}:{config.bridge.port}"),
                        Secrets.require("MT5_BRIDGE_SECRET"), timeout=config.bridge.timeout_seconds)


def build_broker(config: AppConfig, db) -> Broker:
    """PAPER -> internal simulator fed by the configured data source.
    DEMO/LIVE -> MetaTrader (direct on Windows, or through the gateway)."""
    if config.mode is TradingMode.PAPER:
        source: MarketDataSource
        if config.paper.data_source == "synthetic":
            log.warning("PAPER mode on the SYNTHETIC feed: prices are generated, not real market data")
            source = SyntheticFeed(config.symbols, seed=config.paper.synthetic_seed,
                                   speed=config.paper.synthetic_speed, account_currency=config.paper.currency,
                                   spread_points=dict(config.costs.spread_points))
        elif config.paper.data_source == "mt5":
            source = _mt5_broker(config, db, read_only=True)        # quotes only; cannot send orders
        else:
            source = _ReadOnly(_bridge_broker(config))
        broker = PaperBroker(data=source, balance=config.paper.initial_balance, currency=config.paper.currency,
                             leverage=config.paper.leverage, cost=CostModel.from_config(config.costs),
                             on_change=lambda state: db.set_state("paper_broker", state))
        broker.load_state(db.get_state("paper_broker"))
        return broker
    if config.mode in (TradingMode.DEMO, TradingMode.LIVE):
        return _mt5_broker(config, db, read_only=False) if config.broker_backend == "mt5" else _bridge_broker(config)
    raise ValueError(f"no broker for mode {config.mode.value}")


class _ReadOnly(MarketDataSource):
    """Exposes only the market-data half of a broker (used to feed PAPER
    mode from a real terminal without any possibility of sending orders)."""

    def __init__(self, inner: Broker) -> None:
        self._inner = inner

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    def connect(self) -> bool:
        return self._inner.connect()

    def symbol_spec(self, symbol):
        return self._inner.symbol_spec(symbol)

    def tick(self, symbol):
        return self._inner.tick(symbol)

    def rates(self, symbol, timeframe, count):
        return self._inner.rates(symbol, timeframe, count)


def build_news(config: AppConfig, db) -> Optional[NewsMonitor]:
    if not config.news.enabled:
        return None
    if config.mode is TradingMode.PAPER and config.paper.data_source == "synthetic":
        log.info("news filter off: the synthetic feed runs on a virtual clock unrelated to the real calendar")
        return None
    providers = build_providers(config.news.providers, config.news.calendar_file)
    return NewsMonitor(config.news, db, providers)


def build_engine(config: AppConfig, db, notifier: Optional[Notifier] = None,
                 models_dir: Optional[Path] = None) -> TradingEngine:
    broker = build_broker(config, db)
    return TradingEngine(config, broker, db, notifier=notifier or Notifier.from_env(db),
                         news=build_news(config, db), models_dir=models_dir or MODELS_DIR)
