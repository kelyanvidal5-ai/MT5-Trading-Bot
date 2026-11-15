"""The live-trading gate.

Real-money execution is impossible unless ALL of the following hold at the
same time. No single switch, config edit or dashboard click can enable it.

1. ``mode: live`` in the configuration.
2. Environment variable ``MT5QUANT_LIVE_AUTHORIZED`` equals the exact phrase
   ``I_AUTHORIZE_REAL_MONEY_TRADING``.
3. A git-ignored authorization file exists, names the exact account login,
   and is not older than ``demo_gate.authorization_valid_days``. It can only
   be created by the interactive ``python main.py authorize-live`` command,
   which requires the operator to type the confirmation by hand.
4. The database shows a DEMO track record that satisfies ``demo_gate``
   (minimum days and trades).

The gate is checked when the engine starts, when the broker connects, and
again inside the broker adapter for every single order (defence in depth).
Nothing in the AI engine, the dashboard or the strategies can alter it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..core.config import LIVE_AUTH_PHRASE, ROOT, AppConfig, Secrets
from ..core.models import UTC, AccountInfo, TradingMode, utcnow

AUTH_FILE = ROOT / "config" / "live_authorization.json"


class LiveTradingLocked(RuntimeError):
    pass


class LiveGate:
    def __init__(self, config: AppConfig, db=None, auth_file: Optional[Path] = None,
                 require_track_record: bool = True) -> None:
        self.config = config
        self.db = db
        self.auth_file = Path(auth_file) if auth_file else AUTH_FILE
        self.require_track_record = require_track_record

    def read_authorization(self) -> Optional[dict]:
        if not self.auth_file.exists():
            return None
        try:
            return json.loads(self.auth_file.read_text())
        except (OSError, ValueError):
            return None

    def check(self, account: Optional[AccountInfo]) -> tuple[bool, list[str]]:
        """Returns (allowed, reasons_blocking)."""
        reasons: list[str] = []
        if self.config.mode is not TradingMode.LIVE:
            reasons.append("configuration mode is not 'live'")
        if Secrets.get("MT5QUANT_LIVE_AUTHORIZED") != LIVE_AUTH_PHRASE:
            reasons.append("environment authorization phrase is missing")
        auth = self.read_authorization()
        if auth is None:
            reasons.append("no live authorization file (run: python main.py authorize-live)")
        else:
            try:
                signed = datetime.fromisoformat(auth["authorized_at"])
                if signed.tzinfo is None:
                    signed = signed.replace(tzinfo=UTC)
                age = utcnow() - signed
                if age > timedelta(days=self.config.demo_gate.authorization_valid_days) or age < timedelta(0):
                    reasons.append("live authorization has expired")
                if account is not None and str(auth.get("account_login")) != str(account.login):
                    reasons.append("live authorization is for a different account")
                if auth.get("statement") != LIVE_AUTH_PHRASE:
                    reasons.append("live authorization statement is invalid")
            except (KeyError, ValueError, TypeError):
                reasons.append("live authorization file is malformed")
        if self.require_track_record:
            if self.db is None:
                reasons.append("no database available to verify the demo track record")
            else:
                rec = self.db.demo_track_record()
                gate = self.config.demo_gate
                if rec["trades"] < gate.min_demo_trades:
                    reasons.append(f"demo track record too short: {rec['trades']} trades "
                                   f"(< {gate.min_demo_trades})")
                if rec["days"] < gate.min_demo_days:
                    reasons.append(f"demo track record too short: {rec['days']:.0f} days "
                                   f"(< {gate.min_demo_days})")
        return (not reasons), reasons

    def assert_can_trade(self, mode: TradingMode, account: AccountInfo) -> None:
        """Raise unless orders may be sent to ``account`` in ``mode``."""
        if mode in (TradingMode.PAPER, TradingMode.BACKTEST):
            raise LiveTradingLocked(f"mode '{mode.value}' never sends orders to a broker")
        if mode is TradingMode.DEMO:
            if not account.is_demo:
                raise LiveTradingLocked(
                    "mode is 'demo' but the connected account is NOT a demo account - refusing to trade")
            return
        ok, reasons = self.check(account)
        if not ok:
            raise LiveTradingLocked("live trading is locked: " + "; ".join(reasons))


def write_authorization(account_login: str, operator: str, auth_file: Optional[Path] = None) -> Path:
    """Create the authorization file. Called only from the interactive CLI."""
    path = Path(auth_file) if auth_file else AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "account_login": str(account_login),
        "authorized_by": operator,
        "authorized_at": utcnow().isoformat(),
        "statement": LIVE_AUTH_PHRASE,
    }, indent=2))
    return path
