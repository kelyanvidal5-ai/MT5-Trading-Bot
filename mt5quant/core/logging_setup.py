"""Logging with secret redaction.

Every handler gets a filter that masks known secret values and common
credential patterns before a record is emitted, so passwords, tokens and
account credentials cannot reach log files even if a caller logs them by
mistake.
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Secrets

_PATTERNS = [
    # key=value / key: value / "key": "value" for sensitive key names
    re.compile(
        r"""(?ix)
        (["']?(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|signature)["']?
        \s*[:=]\s*["']?)
        ([^\s"',;&}]+)
        """
    ),
    re.compile(r"(?i)(bot)(\d{6,}:[A-Za-z0-9_-]{20,})"),          # telegram bot token in URLs
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
]


def redact(text: str) -> str:
    for value in Secrets.all_values():
        if value in text:
            text = text.replace(value, "***")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "***", text)
    return text


def mask_login(login: object) -> str:
    """Account numbers are shown as ``12****89`` in logs and on the dashboard."""
    s = str(login)
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}{'*' * (len(s) - 4)}{s[-2:]}"


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class _RedactingFormatter(logging.Formatter):
    def formatException(self, ei) -> str:  # noqa: N802
        return redact(super().formatException(ei))


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO", console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = _RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    handlers: list[logging.Handler] = []
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    handlers.append(RotatingFileHandler(path / "mt5quant.log", maxBytes=5_000_000, backupCount=10,
                                        encoding="utf-8"))
    err = RotatingFileHandler(path / "errors.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    err.setLevel(logging.WARNING)
    handlers.append(err)
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactionFilter())
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
