"""Telegram alerter (plan §8, milestone 9).

Read-only-safe: sends a message, never receives commands or holds funds.
Credentials (``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``) come from the
environment. Missing/blank credentials -> ``enabled=False`` no-op, so the
console path works with no Telegram setup at all.

An alert delivery failure must never crash a cycle: ``send`` lets exceptions
propagate to the alert stage, which catches and counts them (the opportunity
is still recorded, just not confirmed-delivered to this channel).
"""

from __future__ import annotations

import os

import httpx

from arbdetector.alerting.format import format_opportunity

_API_BASE = "https://api.telegram.org"
_TIMEOUT_SEC = 10.0


class TelegramAlerter:
    """Posts alerts to one Telegram chat via the Bot API ``sendMessage``."""

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        http_client: httpx.Client | None = None,
        base_url: str = _API_BASE,
    ) -> None:
        self._token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self.enabled = bool(self._token and self._chat_id)
        self._http = http_client or httpx.Client(base_url=base_url, timeout=_TIMEOUT_SEC)

    @classmethod
    def from_env(cls) -> "TelegramAlerter":
        return cls(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        )

    def send(self, summary: dict, *, is_update: bool) -> None:
        if not self.enabled:
            return
        text = format_opportunity(summary, is_update=is_update, plain=True)
        response = self._http.post(
            f"/bot{self._token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
        )
        response.raise_for_status()

    def close(self) -> None:
        self._http.close()
