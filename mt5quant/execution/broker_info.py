"""Read the facts the platform needs from the broker instead of asking the
operator to look them up: exact symbol names, account currency and leverage,
contract specifications and the spreads actually quoted.

Everything here is read-only and reports only what the terminal returned.
"""
from __future__ import annotations

from typing import Optional

from ..core.logging_setup import mask_login
from .broker import Broker, BrokerError


def find_symbols(broker: Broker, pattern: str = "*") -> list[dict]:
    """Symbols offered by the broker whose name matches ``pattern``
    (wildcards allowed, e.g. ``*EUR*``)."""
    lister = getattr(broker, "list_symbols", None)
    if lister is None:
        raise BrokerError("this connection cannot list symbols")
    return lister(pattern)


def describe(broker: Broker, symbols: list[str], bars: int = 2000, timeframe: str = "M5") -> dict:
    """Account facts plus, per symbol, its specification and measured spread."""
    acct = broker.account_info()
    out: dict = {
        "account": {"login": mask_login(acct.login), "server": acct.server, "currency": acct.currency,
                    "leverage": acct.leverage, "type": "demo" if acct.is_demo else "REAL",
                    "balance": acct.balance, "trade_allowed": acct.trade_allowed},
        "symbols": {}, "missing": [],
    }
    for name in symbols:
        try:
            spec = broker.symbol_spec(name)
        except (BrokerError, ValueError) as exc:
            out["missing"].append({"symbol": name, "error": str(exc)})
            continue
        row: dict = {
            "digits": spec.digits, "contract_size": spec.contract_size, "volume_min": spec.volume_min,
            "volume_step": spec.volume_step, "stops_level_points": spec.stops_level,
            "base": spec.currency_base, "quote": spec.currency_profit, "asset_class": spec.asset_class,
            "trade_allowed": spec.trade_allowed, "spread_now_points": None, "spread_median_points": None,
            "spread_p90_points": None, "bars_available": 0,
        }
        try:
            tick = broker.tick(name)
            row["spread_now_points"] = round(tick.spread / spec.point, 1)
        except BrokerError:
            pass
        try:
            frame = broker.rates(name, timeframe, bars)
            row["bars_available"] = int(len(frame))
            if "spread" in frame.columns and len(frame):
                sp = frame["spread"][frame["spread"] > 0]
                if len(sp):
                    row["spread_median_points"] = round(float(sp.median()), 1)
                    row["spread_p90_points"] = round(float(sp.quantile(0.9)), 1)
        except BrokerError:
            pass
        out["symbols"][name] = row
    return out


def suggested_config(info: dict, mode: Optional[str] = None) -> str:
    """A ``config/local.yaml`` fragment built from measured values. Spreads use
    the 90th percentile so backtests lean pessimistic rather than optimistic."""
    names = list(info["symbols"])
    lines = ["# Generated from values read in the terminal (python main.py broker-info)."]
    if mode:
        lines.append(f"mode: {mode}")
    lines.append("symbols: [" + ", ".join(names) + "]")
    lines.append("paper:")
    lines.append(f"  currency: {info['account']['currency']}")
    lines.append(f"  leverage: {min(int(info['account']['leverage']), 500)}")
    spreads = {n: (r["spread_p90_points"] or r["spread_median_points"] or r["spread_now_points"])
               for n, r in info["symbols"].items()}
    known = {n: v for n, v in spreads.items() if v}
    if known:
        lines.append("costs:")
        lines.append("  spread_points:")
        for n, v in known.items():
            lines.append(f"    {n}: {v:g}")
        lines.append("  # commission_per_lot_per_side: set this from your account type's conditions")
    return "\n".join(lines) + "\n"
