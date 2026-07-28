"""Recovery / cleanup on mid-flow failure.

Real cleanup (release held seats, cancel a pending order) depends on endpoints
discovered during recon. Until then we ship a logging NoopRecover so the flow
has something to call; swap in a concrete Recover once docs/api-map.md is
filled. Never silently swallow — every cleanup attempt is observable.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("cinebot.recover")


class Recover(Protocol):
    async def release_seats(self, hold_handles: list[Any]) -> bool: ...
    async def cancel_order(self, order_id: str | None) -> bool: ...


class NoopRecover:
    """Default: logs what would be cleaned up. Safe to call at any time."""

    async def release_seats(self, hold_handles: list[Any]) -> bool:
        log.info("release_seats(stub): would release %d handle(s)", len(hold_handles))
        return True

    async def cancel_order(self, order_id: str | None) -> bool:
        log.info("cancel_order(stub): would cancel order %r", order_id)
        return True


class CompositeRecover:
    """Run multiple recover strategies (e.g. seats-then-order) in order."""

    def __init__(self, *strategies: Recover):
        self._strategies = strategies

    async def release_seats(self, hold_handles: list[Any]) -> bool:
        ok = True
        for s in self._strategies:
            ok = (await s.release_seats(hold_handles)) and ok
        return ok

    async def cancel_order(self, order_id: str | None) -> bool:
        ok = True
        for s in self._strategies:
            ok = (await s.cancel_order(order_id)) and ok
        return ok
