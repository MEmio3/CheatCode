"""In-process event bus and OTP request/response broker.

The flow publishes StepEvents to the bus; the UI subscribes via SSE. OTP is a
request/response pair: the flow awaits a Future keyed by request_id, the UI
POSTs the user-typed code back to resolve it. This keeps the whole control path
async and testable without a real booking backend.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Awaitable, Callable, Optional

from .steps import OtpPurpose, StepEvent, make_event

Clock = Callable[[], float]

# Which catalog step corresponds to each OTP purpose (so the broker emits the
# right labeled event without callers having to know the step id).
_OTP_STEP: dict[OtpPurpose, str] = {
    OtpPurpose.CINEPLEX_LOGIN: "login.waiting_otp",
    OtpPurpose.BKASH_PAYMENT: "payment.waiting_otp",
}


class EventBus:
    """Async fan-out for StepEvents with history replay for late subscribers."""

    def __init__(self, clock: Clock = time.time, history_limit: int = 500, session_id: Optional[str] = None):
        self._clock = clock
        self._subs: list[asyncio.Queue] = []
        self.history: list[StepEvent] = []
        self._history_limit = history_limit
        self.session_id = session_id

    def subscribe(self) -> "asyncio.Queue[StepEvent]":
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        for e in self.history:  # replay so a page refresh keeps its place
            q.put_nowait(e)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    async def publish(self, event: StepEvent) -> None:
        if event.ts == 0.0:
            event.ts = self._clock()
        if event.session_id is None:
            event.session_id = self.session_id
        self.history.append(event)
        if len(self.history) > self._history_limit:
            del self.history[: len(self.history) - self._history_limit]
        for q in self._subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait(event)

    def publish_nowait(self, event: StepEvent) -> None:
        """Sync emit for callers not in a coroutine. Loop must be running."""
        if event.ts == 0.0:
            event.ts = self._clock()
        if event.session_id is None:
            event.session_id = self.session_id
        self.history.append(event)
        for q in self._subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


class OtpTimedOut(Exception):
    """Raised when an OTP is not supplied within the timeout."""


class OtpBroker:
    """Request an OTP from the user (manual UI entry or Telegram relay).

    Channels:
      - "manual":   UI shows a prompt card; user types the code.
      - "telegram": a relay on the user's phone forwards the SMS code.

    On timeout, raises OtpTimedOut (callers decide whether to retry or abort).
    """

    def __init__(
        self,
        bus: EventBus,
        telegram: Optional["OtpChannel"] = None,
        default_timeout: float = 180.0,
        clock: Clock = time.time,
    ):
        self.bus = bus
        self.telegram = telegram
        self.default_timeout = default_timeout
        self._clock = clock
        self._pending: dict[str, asyncio.Future] = {}

    async def request(
        self,
        purpose: OtpPurpose,
        *,
        channel: str = "manual",
        timeout: Optional[float] = None,
        detail: Optional[str] = None,
    ) -> str:
        timeout = timeout if timeout is not None else self.default_timeout
        loop = asyncio.get_running_loop()
        req_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        event = make_event(
            _OTP_STEP[purpose],
            detail=f"otp:{req_id}" + (f"|{detail}" if detail else f"|{channel}"),
            otp_channel=channel,
            ts=0.0,
        )
        await self.bus.publish(event)

        if channel == "telegram" and self.telegram is not None:
            await self.telegram.watch(req_id, purpose, timeout)

        try:
            code = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise OtpTimedOut(f"{purpose.value} not received within {timeout}s") from e
        finally:
            self._pending.pop(req_id, None)
        return code.strip()

    def submit(self, req_id: str, code: str) -> bool:
        fut = self._pending.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result(code)
            return True
        return False

    def cancel(self, req_id: str) -> None:
        fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def has_pending(self) -> bool:
        return bool(self._pending)


class OtpChannel:
    """Interface for an OTP delivery channel (e.g. Telegram SMS relay)."""

    async def watch(self, req_id: str, purpose: OtpPurpose, timeout: float) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass
