"""Multi-session coordinator.

Runs one Runner per real attendee profile, concurrently. Each session has its
own event bus, OTP broker, hold timer, and step stream — tagged with the
profile id so the UI can render per-account progress and OTP prompts.

Seat splitting (Model A — pick once, auto-split): the user picks N total seats
on the shared map; launch() carves them into contiguous chunks of <=10 and
hands one chunk to each account's session. So 30 picked seats + 3 accounts ->
3 sessions of 10, sitting in adjacent blocks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .flow import MockFlow, RunConfig, ScheduleFilter, demo_seat_map
from .profiles import Profile, ProfileStore
from .runner import Runner, RunnerBusy
from .seats.scorer import SeatPreference

log = logging.getLogger("cinebot.multi")

SITE_CAP = 10  # hard per-account per-booking cap; never exceeded


@dataclass
class LaunchPlan:
    session_id: str
    label: str
    seats: list[str]


def chunk_seats(seats: list[str], per_session: int = SITE_CAP) -> list[list[str]]:
    """Split a flat seat list into contiguous chunks of <= per_session."""
    if per_session <= 0:
        raise ValueError("per_session must be > 0")
    return [seats[i : i + per_session] for i in range(0, len(seats), per_session)]


def split_seats_for_guests(
    seats: list[str], seat_map, cap: int = SITE_CAP
) -> list[list[str]]:
    """Split picked seats into guest buckets of <= cap, keeping same-row picks
    together when possible.

    "10 in G + 4 in H" -> [["G..x10"], ["H..x4"]] (two guests). Within a row
    that exceeds cap, the row is split into cap-sized pieces. Runs are then
    greedily packed into buckets so we don't burn a whole guest on a partial
    row when seats could share a transaction (never exceeding cap).
    """
    from collections import defaultdict

    id_to_seat = {s.seat_id: s for s in seat_map.seats}
    by_row: dict = defaultdict(list)
    unknown: list[str] = []
    for sid in seats:
        s = id_to_seat.get(sid)
        if s is None:
            unknown.append(sid)
        else:
            by_row[s.row].append((s.col, sid))

    runs: list[list[str]] = []
    for row in sorted(by_row):
        ids = [sid for _, sid in sorted(by_row[row])]
        for i in range(0, len(ids), cap):
            runs.append(ids[i : i + cap])
    for sid in unknown:
        runs.append([sid])

    buckets: list[list[str]] = []
    for run in runs:
        if buckets and len(buckets[-1]) + len(run) <= cap:
            buckets[-1].extend(run)
        else:
            buckets.append(list(run))
    return buckets or [list(seats)]


class MultiRunner:
    def __init__(self, profiles: Optional[ProfileStore] = None, seat_map=None) -> None:
        self.profiles = profiles or ProfileStore()
        self._seat_map = seat_map  # callable returning the current SeatMap, or None
        self.sessions: dict[str, Runner] = {}  # profile.id -> active Runner

    def seat_map(self):
        return self._seat_map() if callable(self._seat_map) else None

    def get(self, session_id: str) -> Optional[Runner]:
        return self.sessions.get(session_id)

    def plan(self, profile_ids: list[str], seats: list[str], *, guest: bool = False) -> list[LaunchPlan]:
        """Validate + split seats across identities. Guest mode auto-splits
        into <=10 buckets (row-aware) and uses the first N identities."""
        if not profile_ids:
            raise ValueError("no identities selected")
        if not seats:
            raise ValueError("no seats selected — pick seats on the map first")
        profiles = {p.id: p for p in self.profiles.list()}
        missing = [pid for pid in profile_ids if pid not in profiles]
        if missing:
            raise ValueError(f"unknown profile ids: {missing}")
        if guest:
            sm = self.seat_map()
            if sm is None:
                raise ValueError("seat map not available; can't split for guest mode")
            buckets = split_seats_for_guests(seats, sm)
        else:
            buckets = chunk_seats(seats)
        if len(profile_ids) < len(buckets):
            raise ValueError(
                f"{len(seats)} seats split into {len(buckets)} guest session(s) "
                f"(<= {SITE_CAP} each), but only {len(profile_ids)} provided. "
                f"Add {len(buckets) - len(profile_ids)} more guest identity/identities, or reduce seats."
            )
        chosen = profile_ids[: len(buckets)]
        return [
            LaunchPlan(session_id=pid, label=profiles[pid].label, seats=b)
            for pid, b in zip(chosen, buckets)
        ]

    async def launch(
        self,
        profile_ids: list[str],
        *,
        movie: str,
        seats: list[str],
        preference: SeatPreference,
        schedule: ScheduleFilter,
        dry_run: bool,
        otp_channel: str,
        simulate_conflict: bool = False,
        guest: bool = False,
    ) -> list[str]:
        plans = self.plan(profile_ids, seats, guest=guest)
        launched: list[str] = []
        for p in plans:
            # one booking per profile at a time; replace any finished session
            existing = self.sessions.get(p.session_id)
            if existing and existing.is_busy:
                raise RunnerBusy(f"profile {p.label!r} already has a booking running")
            runner = Runner(MockFlow(seat_map=self.seat_map()), session_id=p.session_id)
            config = RunConfig(
                movie=movie,
                n_tickets=len(p.seats),
                preference=preference,
                schedule=schedule,
                seats=p.seats,
                dry_run=dry_run,
                otp_channel=otp_channel,
                simulate_conflict=simulate_conflict,
                guest=guest,
            )
            await runner.start(config)
            self.sessions[p.session_id] = runner
            launched.append(p.session_id)
            log.info("launched %s session %s (%s) for %d seats", "guest" if guest else "account", p.session_id, p.label, len(p.seats))
        return launched
