"""Telegram OTP relay (optional).

A bot that runs on the user's phone receives SMS forwarded by an SMS-forwarder
app, extracts the code, and submits it to the broker for the currently-watched
request. This only works on Android (iOS sandboxes incoming SMS). python-
telegram-bot is imported lazily so the rest of the app runs without it.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..events import OtpBroker, OtpChannel
from ..steps import OtpPurpose

log = logging.getLogger("cinebot.otp.telegram")

# OTP codes are typically 4-6 digits; accept up to 8 to be safe.
_CODE_RE = re.compile(r"\b(\d{4,8})\b")


class TelegramRelay(OtpChannel):
    """Wires incoming Telegram messages to the broker's pending OTP request.

    OTP requests are sequential (login, then payment), so there is at most one
    watched request at a time; any code-like message is routed to it.
    """

    def __init__(self, bot_token: str, broker: OtpBroker):
        self.bot_token = bot_token
        self.broker = broker
        self._watching: Optional[str] = None
        self._purpose: Optional[OtpPurpose] = None
        self._app = None  # telegram.ext.Application (lazy)
        self._available = False

    async def start(self) -> None:
        try:
            from telegram.ext import ApplicationBuilder, MessageHandler, filters
        except ImportError:
            log.warning(
                "python-telegram-bot not installed — Telegram OTP relay disabled. "
                "Falling back to manual entry."
            )
            return
        self._app = (
            ApplicationBuilder().token(self.bot_token).build()
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & (~filters.COMMAND), self._on_message)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        self._available = True
        log.info("Telegram OTP relay started")

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._app = None
            self._available = False

    async def watch(self, req_id: str, purpose: OtpPurpose, timeout: float) -> None:
        self._watching = req_id
        self._purpose = purpose
        log.info("Telegram relay watching for %s code (req %s)", purpose.value, req_id)

    async def _on_message(self, update, context) -> None:  # noqa: ANN001
        if self._watching is None:
            return
        text = (update.effective_message.text or "") if update.effective_message else ""
        m = _CODE_RE.search(text)
        if not m:
            return
        code = m.group(1)
        log.info("Telegram relay received code for %s", self._purpose)
        self.broker.submit(self._watching, code)
        self._watching = None
        self._purpose = None
