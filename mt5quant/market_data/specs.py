"""Generic contract specifications for the SIMULATOR ONLY.

In DEMO/LIVE the specification always comes from the broker's terminal. These
generic values exist so backtests and paper trading can run without a
terminal; they follow common retail FX conventions and are not a statement
about any particular broker.
"""
from __future__ import annotations

from ..core.models import SymbolSpec

_FX_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "SGD", "MXN", "ZAR"}
_APPROX_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067, "CHF": 1.12, "AUD": 0.66,
               "NZD": 0.61, "CAD": 0.73}


def split_pair(symbol: str) -> tuple[str, str]:
    core = symbol[:6].upper()
    return core[:3], core[3:6]


def is_fx_pair(symbol: str) -> bool:
    b, q = split_pair(symbol)
    return b in _FX_CCY and q in _FX_CCY


def generic_spec(symbol: str, account_currency: str = "USD") -> SymbolSpec:
    base, quote = split_pair(symbol)
    if base in ("XAU", "XAG") and quote in _FX_CCY:
        digits, contract = (2, 100.0) if base == "XAU" else (3, 5000.0)
        point = 10 ** -digits
        return SymbolSpec(name=symbol, digits=digits, point=point, tick_size=point,
                          tick_value=point * contract * _to_account(quote, account_currency),
                          contract_size=contract, currency_base=base, currency_profit=quote,
                          currency_margin=base, asset_class="metal", stops_level=0)
    if not is_fx_pair(symbol):
        raise ValueError(
            f"no generic simulator spec for '{symbol}'. Provide the broker specification "
            "(use the MT5 data source, or register a SymbolSpec explicitly)."
        )
    digits = 3 if quote == "JPY" else 5
    point = 10 ** -digits
    contract = 100_000.0
    return SymbolSpec(name=symbol, digits=digits, point=point, tick_size=point,
                      tick_value=point * contract * _to_account(quote, account_currency),
                      contract_size=contract, currency_base=base, currency_profit=quote,
                      currency_margin=base, asset_class="forex", stops_level=0)


def _to_account(ccy: str, account: str) -> float:
    """Rough static conversion used only to seed generic specs; the simulator
    refreshes tick_value from simulated prices whenever it can."""
    if ccy == account:
        return 1.0
    return _APPROX_USD.get(ccy, 1.0) / _APPROX_USD.get(account, 1.0)
