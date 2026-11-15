"""Notifications: Telegram, generic webhook (Slack/Discord compatible) and
SMTP e-mail. Every channel is optional and configured only through
environment variables. Messages are redacted and rate-limited, and a failing
channel never interrupts trading.
"""
from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Callable, Optional

from ..core.config import Secrets
from ..core.logging_setup import redact

log = logging.getLogger(__name__)

LEVELS = {"info": 0, "warning": 1, "critical": 2}
_ICON = {"info": "[info]", "warning": "[WARNING]", "critical": "[CRITICAL]"}


class Channel:
    name = "channel"

    def send(self, level: str, title: str, body: str) -> None:
        raise NotImplementedError


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, post: Optional[Callable] = None) -> None:
        self._token, self._chat = token, chat_id
        self._post = post

    def send(self, level: str, title: str, body: str) -> None:
        import requests

        post = self._post or requests.post
        r = post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                 json={"chat_id": self._chat, "text": f"{_ICON[level]} {title}\n{body}"[:4000]}, timeout=10)
        if getattr(r, "status_code", 200) != 200:
            raise RuntimeError(f"telegram HTTP {r.status_code}")


class WebhookChannel(Channel):
    name = "webhook"

    def __init__(self, url: str, post: Optional[Callable] = None) -> None:
        self._url, self._post = url, post

    def send(self, level: str, title: str, body: str) -> None:
        import requests

        post = self._post or requests.post
        text = f"{_ICON[level]} {title}\n{body}"[:1900]
        r = post(self._url, json={"text": text, "content": text}, timeout=10)
        if getattr(r, "status_code", 200) >= 300:
            raise RuntimeError(f"webhook HTTP {r.status_code}")


class EmailChannel(Channel):
    name = "email"

    def __init__(self, host: str, port: int, user: str, password: str, to: str) -> None:
        self._host, self._port, self._user, self._password, self._to = host, port, user, password, to

    def send(self, level: str, title: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"MT5 Quant {_ICON[level]} {title}"
        msg["From"], msg["To"] = self._user, self._to
        msg.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=15) as s:
            s.starttls()
            s.login(self._user, self._password)
            s.send_message(msg)


class Notifier:
    def __init__(self, channels: Optional[list[Channel]] = None, db=None, min_level: str = "info",
                 min_interval_seconds: float = 300.0, clock: Callable[[], float] = time.time) -> None:
        self.channels = channels or []
        self.db = db
        self.min_level = min_level
        self.min_interval = min_interval_seconds
        self._clock = clock
        self._last: dict[str, float] = {}

    @classmethod
    def from_env(cls, db=None) -> "Notifier":
        ch: list[Channel] = []
        if Secrets.get("TELEGRAM_BOT_TOKEN") and Secrets.get("TELEGRAM_CHAT_ID"):
            ch.append(TelegramChannel(Secrets.get("TELEGRAM_BOT_TOKEN"), Secrets.get("TELEGRAM_CHAT_ID")))
        if Secrets.get("NOTIFY_WEBHOOK_URL"):
            ch.append(WebhookChannel(Secrets.get("NOTIFY_WEBHOOK_URL")))
        if Secrets.get("SMTP_HOST") and Secrets.get("SMTP_USER") and Secrets.get("SMTP_TO"):
            ch.append(EmailChannel(Secrets.get("SMTP_HOST"), int(Secrets.get("SMTP_PORT", "587")),
                                   Secrets.get("SMTP_USER"), Secrets.get("SMTP_PASSWORD"), Secrets.get("SMTP_TO")))
        return cls(ch, db=db)

    def notify(self, level: str, title: str, body: str = "", key: Optional[str] = None) -> bool:
        """Send to all channels. ``key`` enables de-duplication: the same key
        is not re-sent within ``min_interval_seconds`` (critical bypasses)."""
        level = level if level in LEVELS else "info"
        title, body = redact(title), redact(body)
        now = self._clock()
        dedupe = key or title
        if level != "critical" and now - self._last.get(dedupe, -1e18) < self.min_interval:
            return False
        self._last[dedupe] = now
        getattr(log, "critical" if level == "critical" else level)("NOTIFY %s | %s", title, body.replace("\n", " | "))
        delivered = []
        if LEVELS[level] >= LEVELS[self.min_level]:
            for c in self.channels:
                try:
                    c.send(level, title, body)
                    delivered.append(c.name)
                except Exception as exc:              # a broken channel must never stop the engine
                    log.warning("notification channel %s failed: %s", c.name, type(exc).__name__)
        if self.db is not None:
            try:
                self.db.log_notification(level, title, body, ",".join(delivered) or "log")
            except Exception:
                log.exception("could not persist notification")
        return True
