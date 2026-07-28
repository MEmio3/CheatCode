"""Booking flow: config, context, and pluggable flow drivers.

The real driver (HttpFlow / BrowserFlow) is filled in once recon maps the API
(see docs/api-map.md). Until then MockFlow walks the entire step sequence —
login OTP, seat selection via the real scorer, hold timer, payment OTP — so the
UI, event bus, and OTP plumbing can be exercised end to end without touching
the live site.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Optional, Protocol

from .events import EventBus, OtpBroker, OtpTimedOut
from .recover import NoopRecover, Recover
from .seats.scorer import Seat, SeatMap, SeatPreference, find_best_block, seats_by_ids
from .steps import OtpPurpose, Phase, make_event
from .timer import HoldTimer, HoldWindowTooSmall

log = logging.getLogger("cinebot.flow")

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass
class ScheduleFilter:
    date: Optional[str] = None  # YYYY-MM-DD
    after_time: Optional[str] = None  # HH:MM (24h)
    branch: Optional[str] = None  # e.g. "Bashundhara City"


@dataclass
class RunConfig:
    movie: str
    n_tickets: int  # 1..10
    preference: SeatPreference = field(default_factory=SeatPreference)
    schedule: ScheduleFilter = field(default_factory=ScheduleFilter)
    seats: Optional[list[str]] = None  # explicit seat ids -> override auto-pick
    dry_run: bool = True  # default safe: never reach real payment
    otp_channel: str = "manual"  # "manual" | "telegram"
    simulate_conflict: bool = False  # demo: pretend the first picked seat got taken at hold time
    guest: bool = False  # guest checkout: skip account login, only bKash payment OTP at the end

    def __post_init__(self) -> None:
        if not 1 <= self.n_tickets <= 10:
            raise ValueError(f"n_tickets must be 1..10 (site cap), got {self.n_tickets}")
        if self.otp_channel not in ("manual", "telegram"):
            raise ValueError(f"unknown otp_channel {self.otp_channel!r}")
        if self.seats is not None and len(self.seats) != self.n_tickets:
            raise ValueError(
                f"seats count ({len(self.seats)}) must equal n_tickets ({self.n_tickets})"
            )


@dataclass
class RunResult:
    ok: bool
    seats: list[Seat] = field(default_factory=list)
    order_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class FlowContext:
    bus: EventBus
    otp: OtpBroker
    timer: HoldTimer
    recover: Recover = field(default_factory=NoopRecover)
    clock: Clock = time.time
    sleep: Sleeper = asyncio.sleep


class Flow(Protocol):
    async def run(self, config: RunConfig, ctx: FlowContext) -> RunResult: ...


# ---- demo seat map (used by MockFlow + dry-run) --------------------------====


def demo_seat_map(n_rows: int = 12, n_cols: int = 16, seed: int = 7) -> SeatMap:
    """A deterministic pretend seat map: one center aisle, some taken seats.

    The first row (row 0) is nearest the screen. Real seat maps come from the
    API post-recon; this exists so selection + UI work before that.
    """
    import random

    rng = random.Random(seed)
    seats: list[Seat] = []
    aisle_col = n_cols // 2
    # make rows around 60-70% back mostly empty (popular, but available)
    for r in range(n_rows):
        for c in range(n_cols):
            is_aisle = c == aisle_col
            # taken ~ 35% of non-aisle seats, skewed: front rows fuller
            base = 0.55 if r < n_rows * 0.4 else 0.25
            available = (not is_aisle) and rng.random() > base
            seats.append(
                Seat(
                    row=r,
                    col=c,
                    row_label=chr(ord("A") + r),
                    col_label=str(c + 1),
                    available=available,
                    is_aisle=is_aisle,
                    seat_id=f"{chr(ord('A') + r)}{c + 1}",
                )
            )
    return SeatMap(n_rows=n_rows, n_cols=n_cols, seats=seats)


# ---- mock flow -----------------------------------------------------------====


class MockFlow:
    """Walks the full step sequence using the real scorer + OTP broker.

    Does NOT hit the network. Use it to develop and demo the UI; swap in the
    real driver after recon. Step pacing is configurable for fast tests.
    """

    def __init__(self, seat_map: Optional[SeatMap] = None, step_delay: float = 0.7, contention_seats: Optional[set] = None):
        self.seat_map = seat_map or demo_seat_map()
        self.step_delay = step_delay
        self.contention_seats: set[str] = set(contention_seats or set())

    def _map_excluding(self, ids: set[str]) -> SeatMap:
        """A copy of the map with the given seat ids marked unavailable.

        Models re-fetching a stale map after a conflict so auto-repick avoids
        seats we already know are gone.
        """
        if not ids:
            return self.seat_map
        seats = [replace(s, available=False) if s.seat_id in ids else s for s in self.seat_map.seats]
        return SeatMap(n_rows=self.seat_map.n_rows, n_cols=self.seat_map.n_cols, seats=seats)

    async def _emit(self, ctx: FlowContext, step_id: str, detail: Optional[str] = None) -> None:
        await ctx.bus.publish(make_event(step_id, detail=detail, ts=0.0))
        await ctx.sleep(self.step_delay)

    async def run(self, config: RunConfig, ctx: FlowContext) -> RunResult:
        try:
            # 1. session
            await self._emit(ctx, "session.restore", detail="guest checkout" if config.guest else None)

            # 2. login OTP - skipped in guest mode (no account to log into)
            if not config.guest:
                await self._emit(ctx, "login.request_otp")
                try:
                    code = await ctx.otp.request(
                        OtpPurpose.CINEPLEX_LOGIN, channel=config.otp_channel
                    )
                except OtpTimedOut as e:
                    return RunResult(ok=False, error=str(e))
                log.info("login OTP accepted: %s", code)

            # 3. find movie + showtimes
            await self._emit(ctx, "browse.find_movie", detail=config.movie)
            await self._emit(
                ctx,
                "browse.showtimes",
                detail=(
                    f"branch={config.schedule.branch or 'any'} "
                    f"date={config.schedule.date or 'any'} "
                    f"after={config.schedule.after_time or 'any'}"
                ),
            )

            # 4+5. seats + hold, with conflict recovery.
            # The map is a snapshot. Between pick and hold someone else may grab
            # a seat; the hold call is where availability is actually confirmed.
            #   manual pick -> surface the conflict, never silently substitute
            #   auto pick    -> exclude the taken seat, re-pick next-best, retry
            manual = bool(config.seats)
            contention: set[str] = set(self.contention_seats)
            if config.simulate_conflict and config.seats:
                contention.add(config.seats[0])  # demo: first picked seat "just taken"

            held: Optional[list[Seat]] = None
            label = ""
            taken_now: set[str] = set()
            max_attempts = 3
            for attempt in range(max_attempts):
                await self._emit(
                    ctx, "seats.load_map",
                    detail=f"attempt {attempt + 1}/{max_attempts}" if attempt else None,
                )
                active_map = self._map_excluding(taken_now)
                if manual and attempt == 0:
                    block = seats_by_ids(active_map, config.seats)
                    if block is None:
                        return RunResult(ok=False, error="One or more selected seats not found on the map")
                    bad = [s for s in block if not s.available or s.is_aisle]
                    if bad:
                        names = ", ".join(f"{s.row_label}{s.col_label}" for s in bad)
                        return RunResult(ok=False, error=f"Selected seats unavailable: {names}")
                else:
                    block = find_best_block(active_map, config.n_tickets, config.preference)
                    if block is None:
                        return RunResult(ok=False, error="No contiguous block of seats available")
                label = ", ".join(f"{s.row_label}{s.col_label}" for s in block)
                tag = "(your pick)" if (manual and attempt == 0) else ("(re-picked)" if attempt else "(scored best)")
                await self._emit(ctx, "seats.select", detail=f"{label} {tag}")

                # hold (arm the timer — the architectural driver)
                await ctx.bus.publish(make_event("hold.seats", detail=label, ts=0.0))
                ctx.timer.arm(ttl_seconds=300)  # placeholder; real TTL from recon
                await ctx.sleep(self.step_delay)

                lost = [s.seat_id for s in block if s.seat_id in contention]
                if not lost:
                    held = block
                    break
                # conflict: one of these was booked between pick and hold
                taken_now.update(lost)
                lost_label = ", ".join(lost)
                if manual and attempt == 0:
                    return RunResult(
                        ok=False,
                        error=f"Someone just booked: {lost_label}. Re-pick on the map and launch again.",
                    )
                await self._emit(
                    ctx, "seats.select", detail=f"CONFLICT: {lost_label} taken - auto re-picking"
                )

            if held is None:
                return RunResult(ok=False, error="Seats kept getting taken; try again.")
            block = held
            label = ", ".join(f"{s.row_label}{s.col_label}" for s in block)

            # 6. order
            ctx.timer.guard(needed_seconds=120, label="payment OTP + bKash auth")
            await self._emit(ctx, "order.create")

            # 7. payment
            await self._emit(ctx, "payment.init")
            if config.dry_run:
                # stop before any real money moves
                await ctx.bus.publish(
                    make_event(
                        "done",
                        detail=f"DRY RUN — held {label}; no payment attempted",
                        ts=0.0,
                    )
                )
                return RunResult(ok=True, seats=block, order_id=None)

            try:
                pay_code = await ctx.otp.request(
                    OtpPurpose.BKASH_PAYMENT, channel=config.otp_channel
                )
            except OtpTimedOut as e:
                await ctx.recover.release_seats([{"seats": block}])
                return RunResult(ok=False, error=str(e))
            log.info("payment OTP accepted: %s", pay_code)

            await self._emit(ctx, "payment.confirm")
            await self._emit(ctx, "done", detail=f"Booked {label}")
            return RunResult(ok=True, seats=block, order_id="mock-order")

        except HoldWindowTooSmall as e:
            await ctx.recover.release_seats([])
            return RunResult(ok=False, error=f"Hold window too small: {e}")
        except Exception as e:  # never leave a partial hold dangling
            log.exception("mock flow failed")
            try:
                await ctx.recover.release_seats([])
            except Exception:
                pass
            return RunResult(ok=False, error=str(e))
