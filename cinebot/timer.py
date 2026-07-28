"""Hold-expiry coordinator.

The seat hold has a fixed TTL. OTP delivery + Telegram relay + bKash
authorization all have to complete inside that window, so the deadline is an
explicit, first-class object the flow checks before every slow step — never an
implicit assumption. (Measured during recon; see docs/api-map.md.)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .events import EventBus
from .steps import error_event

Clock = Callable[[], float]


class HoldWindowTooSmall(Exception):
    """Raised when a needed step cannot fit inside the remaining hold window."""


@dataclass
class HoldBudget:
    ttl: float
    deadline: float
    clock: Clock

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def fraction_left(self) -> float:
        if self.ttl <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining() / self.ttl))

    def expired(self) -> bool:
        return self.remaining() <= 0


class HoldTimer:
    """Owns the hold deadline. Call `guard()` before any slow step."""

    def __init__(self, bus: EventBus, clock: Clock = time.time):
        self.bus = bus
        self._clock = clock
        self._budget: Optional[HoldBudget] = None

    def arm(self, ttl_seconds: float) -> HoldBudget:
        """Start the clock. Returns the live budget (also stashed internally)."""
        self._budget = HoldBudget(
            ttl=ttl_seconds,
            deadline=self._clock() + ttl_seconds,
            clock=self._clock,
        )
        return self._budget

    @property
    def budget(self) -> Optional[HoldBudget]:
        return self._budget

    def guard(self, needed_seconds: float, *, label: str) -> None:
        """Raise HoldWindowTooSmall if `needed_seconds` won't fit before expiry.

        Call this right before an OTP wait or a payment leg so the failure mode
        is explicit rather than a mysterious mid-payment seat release.
        """
        if self._budget is None:
            return  # no hold armed yet (e.g. pre-hold steps) — nothing to guard
        if self._budget.expired() or self._budget.remaining() < needed_seconds:
            raise HoldWindowTooSmall(
                f"'{label}' needs ~{needed_seconds:.0f}s but only "
                f"{self._budget.remaining():.0f}s remain in the hold window"
            )

    def extend(self, extra_seconds: float) -> None:
        if self._budget is not None:
            self._budget.deadline += extra_seconds

    async def report_expired(self, detail: str) -> None:
        await self.bus.publish(error_event(f"Hold expired: {detail}", ts=self._clock()))
