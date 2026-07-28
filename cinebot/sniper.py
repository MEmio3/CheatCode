"""Auto-sniper: watch the public schedule until a target movie becomes bookable
on a specific date/hall/time window, then fire a four-session group booking for
a live-computed block of seats.

Seat plan is a rule, not a fixed label list, so it adapts to the new show's
layout when the target drops:

    take all of the primary rows (default E and F) except the last `trim_last`
    seats of each, then fill the remainder from `fill_row` (default G) until
    `total_seats` (default 36). The assembled labels are split into <=10 chunks
    and zipped one-to-one with the four attendees.

The target + attendees are persisted to ``snipe_config.json`` (gitignored — it
holds attendee names and bKash numbers). The watcher reuses CatalogManager to
poll and GroupBookingManager to fire, so it owns no booking logic of its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .group import GroupPlanError, movie_matches, parse_show_time

log = logging.getLogger("cinebot.sniper")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "snipe_config.json")
ACTIVE_PATH = os.path.join(os.path.dirname(__file__), "..", "snipe_active.json")
SITE_TRANSACTION_CAP = 10


@dataclass
class SnipeAttendee:
    name: str
    bkash: str


@dataclass
class SnipeConfig:
    target_movie: str = "Spider-Man: Brand New Day"
    location_id: int = 1
    location_name: str = "Bashundhara Shopping Mall"
    hall_id: int = 6
    show_date: str = "2026-08-01"  # 1 Aug
    time_start: str = "15:30"  # 3:30 PM
    time_end: str = "18:00"  # 6:00 PM
    poll_seconds: int = 75
    total_seats: int = 36
    primary_rows: list[str] = field(default_factory=lambda: ["E", "F"])
    fill_row: str = "G"
    trim_last: int = 2
    num_payments: int = 4
    attendees: list[SnipeAttendee] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_movie": self.target_movie,
            "location_id": self.location_id,
            "location_name": self.location_name,
            "hall_id": self.hall_id,
            "show_date": self.show_date,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "poll_seconds": self.poll_seconds,
            "total_seats": self.total_seats,
            "primary_rows": list(self.primary_rows),
            "fill_row": self.fill_row,
            "trim_last": self.trim_last,
            "num_payments": self.num_payments,
            "attendees": [{"name": a.name, "bkash": a.bkash} for a in self.attendees],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SnipeConfig":
        return cls(
            target_movie=str(d.get("target_movie") or "Spider-Man: Brand New Day"),
            location_id=int(d.get("location_id") or 1),
            location_name=str(d.get("location_name") or "Bashundhara Shopping Mall"),
            hall_id=int(d.get("hall_id") or 6),
            show_date=str(d.get("show_date") or "2026-08-01"),
            time_start=str(d.get("time_start") or "15:30"),
            time_end=str(d.get("time_end") or "18:00"),
            poll_seconds=int(d.get("poll_seconds") or 75),
            total_seats=int(d.get("total_seats") or 36),
            primary_rows=[str(r) for r in (d.get("primary_rows") or ["E", "F"])],
            fill_row=str(d.get("fill_row") or "G"),
            trim_last=int(d.get("trim_last") or 2),
            num_payments=int(d.get("num_payments") or 4),
            attendees=[
                SnipeAttendee(name=str(a.get("name") or ""), bkash=str(a.get("bkash") or ""))
                for a in (d.get("attendees") or [])
            ],
        )

    def validate(self) -> None:
        if not self.target_movie.strip():
            raise GroupPlanError("Pick a target movie to watch for.")
        if self.location_id <= 0 or self.hall_id <= 0:
            raise GroupPlanError("Location and hall are required.")
        parse_show_time(self.time_start)
        parse_show_time(self.time_end)
        if not (1 <= self.total_seats <= 40):
            raise GroupPlanError("Total seats must be between 1 and 40.")
        if len(self.primary_rows) < 1:
            raise GroupPlanError("Pick at least one primary row.")
        min_payments = (self.total_seats + SITE_TRANSACTION_CAP - 1) // SITE_TRANSACTION_CAP
        if not min_payments <= self.num_payments <= 8:
            raise GroupPlanError(
                f"{self.total_seats} seats need between {min_payments} and 8 payments; "
                f"you set {self.num_payments}."
            )
        if len(self.attendees) != self.num_payments:
            raise GroupPlanError(
                f"{self.num_payments} payment session(s) selected but "
                f"{len(self.attendees)} attendee(s) provided."
            )
        for i, a in enumerate(self.attendees, 1):
            if not a.name.strip():
                raise GroupPlanError(f"Attendee {i} needs a name.")
            if not a.bkash.strip():
                raise GroupPlanError(f"Attendee {i} needs a bKash number.")


def save_config(config: SnipeConfig) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(CONFIG_PATH)), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
    return CONFIG_PATH


def load_config() -> Optional[SnipeConfig]:
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return SnipeConfig.from_dict(json.load(f))
    except Exception as exc:
        log.warning("could not load snipe config: %s", exc)
        return None


def _write_active() -> None:
    """Mark the sniper as intentionally running so it resumes after a reboot."""
    try:
        with open(ACTIVE_PATH, "w", encoding="utf-8") as f:
            json.dump({"active": True, "ts": int(time.time())}, f)
    except Exception as exc:  # pragma: no cover - best-effort marker
        log.warning("could not write active marker: %s", exc)


def _clear_active() -> None:
    try:
        if os.path.exists(ACTIVE_PATH):
            os.remove(ACTIVE_PATH)
    except Exception as exc:  # pragma: no cover
        log.warning("could not clear active marker: %s", exc)


def is_active() -> bool:
    return os.path.exists(ACTIVE_PATH)


def compute_seat_plan(
    seat_catalog: dict[str, Any],
    seat_type_id: int,
    primary_rows: list[str],
    fill_row: str,
    total: int,
    trim_last: int = 2,
) -> list[str]:
    """Resolve the exact seat labels from a live seat map per the plan rule."""
    seat_type = next(
        (st for st in (seat_catalog.get("seat_types") or [])
         if int(st.get("id") or 0) == int(seat_type_id)),
        None,
    )
    if seat_type is None:
        raise GroupPlanError("The chosen seat class is not in the live layout.")

    by_row: dict[str, list[dict[str, Any]]] = {}
    for row in seat_type.get("rows") or []:
        cells = [
            c for c in (row.get("cells") or [])
            if c.get("label") and c.get("status") == "available"
        ]
        cells.sort(key=lambda c: int(c.get("cidx") or 0))
        by_row[str(row.get("label") or "")] = cells

    picked: list[dict[str, Any]] = []
    for r in primary_rows:
        cells = by_row.get(r, [])
        if len(cells) <= trim_last:
            raise GroupPlanError(
                f"Row {r} has only {len(cells)} available seat(s); "
                f"cannot drop the last {trim_last}."
            )
        picked.extend(cells[: len(cells) - trim_last])

    need = total - len(picked)
    if need < 0:
        picked = picked[:total]
    elif need > 0:
        fill_cells = by_row.get(fill_row, [])
        if len(fill_cells) < need:
            raise GroupPlanError(
                f"Need {need} more seat(s) from row {fill_row} to reach {total}, "
                f"only {len(fill_cells)} available there."
            )
        picked.extend(fill_cells[:need])

    labels = [str(c["label"]) for c in picked]
    if len(labels) != total:
        raise GroupPlanError(f"Could not assemble {total} seats (got {len(labels)}).")
    return labels


def chunk_labels_into(labels: list[str], parts: int) -> list[list[str]]:
    """Split labels into exactly `parts` contiguous chunks, as evenly as
    possible. Each chunk stays <= SITE_TRANSACTION_CAP as long as
    parts >= ceil(n/cap) (enforced by SnipeConfig.validate)."""
    parts = max(1, parts)
    base, rem = divmod(len(labels), parts)
    sizes = [base + (1 if i < rem else 0) for i in range(parts)]
    out: list[list[str]] = []
    i = 0
    for size in sizes:
        out.append(labels[i : i + size])
        i += size
    return out


class SniperManager:
    """Owns one watch-and-fire loop. Delegates booking to GroupBookingManager."""

    def __init__(self, catalog, group) -> None:
        self.catalog = catalog
        self.group = group
        self.config: Optional[SnipeConfig] = None
        self.status = "idle"
        self.detail = "No sniper running. Save a target, then start watching."
        self.last_poll_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._user_stop = False

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        ago = None
        if self.last_poll_at is not None:
            ago = max(0, int(time.time() - self.last_poll_at))
        return {
            "status": self.status,
            "detail": self.detail,
            "busy": self.busy,
            "ago_seconds": ago,
            "config": self.config.to_dict() if self.config else None,
        }

    async def start(self, config: SnipeConfig) -> None:
        if self.busy:
            raise GroupPlanError("The sniper is already running.")
        config.validate()
        self.config = config
        save_config(config)
        _write_active()
        self.status = "watching"
        self.detail = (
            f"Watching for '{config.target_movie}' on {config.show_date} at "
            f"{config.location_name} Hall {config.hall_id} "
            f"({config.time_start}-{config.time_end}); {config.total_seats} seats."
        )
        self.last_poll_at = None
        self._task = asyncio.create_task(self._watch(), name="snipe-watch")

    async def stop(self) -> bool:
        if not self.busy:
            return False
        assert self._task is not None
        self._user_stop = True
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def shutdown(self) -> None:
        """Cancel the watcher WITHOUT clearing the active marker, so it resumes
        on the next server start. (Contrast with stop(), which is a user action
        and clears the marker.)"""
        if not self.busy:
            return
        assert self._task is not None
        self._user_stop = False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _watch(self) -> None:
        cfg = self.config
        assert cfg is not None
        try:
            while True:
                try:
                    await self._poll_once(cfg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("sniper poll error: %s", exc)
                    self.detail = f"Poll error: {exc}. Retrying in {cfg.poll_seconds}s."
                if self.status in ("handed_off", "error"):
                    break
                await asyncio.sleep(max(15, cfg.poll_seconds))
        except asyncio.CancelledError:
            self.status = "stopped"
            self.detail = "Sniper stopped."
            raise
        finally:
            # Keep the marker across a server shutdown/crash so the watch resumes
            # on the next boot. Clear it on a real stop, a successful hand-off,
            # or a terminal error.
            keep_for_resume = self.status == "stopped" and not self._user_stop
            if not keep_for_resume:
                _clear_active()
            self._user_stop = False

    async def _poll_once(self, cfg: SnipeConfig) -> None:
        self.last_poll_at = time.time()
        self.status = "watching"
        self.detail = f"Polling schedule for '{cfg.target_movie}' on {cfg.show_date}..."

        dates = await self.catalog.dates(cfg.location_id)
        target_day = next((d for d in dates if str(d.get("date") or "") == cfg.show_date), None)
        if target_day is None:
            self.detail = (
                f"{cfg.show_date} is not published at {cfg.location_name} yet. "
                f"Next poll in {cfg.poll_seconds}s."
            )
            return

        movie = next(
            (
                m for m in (target_day.get("movies") or [])
                if movie_matches(str(m.get("title") or ""), cfg.target_movie)
            ),
            None,
        )
        if movie is None or not int(movie.get("id") or 0):
            self.detail = (
                f"'{cfg.target_movie}' is not bookable on {cfg.show_date} yet. "
                f"Next poll in {cfg.poll_seconds}s."
            )
            return

        shows = await self.catalog.shows(cfg.location_id, int(movie["id"]), cfg.show_date)
        chosen = self._pick_show(shows, cfg)
        if chosen is None:
            self.detail = (
                f"'{cfg.target_movie}' is listed on {cfg.show_date}, but no Hall "
                f"{cfg.hall_id} show between {cfg.time_start} and {cfg.time_end}. "
                f"Next poll in {cfg.poll_seconds}s."
            )
            return

        # resolve seats + build the four payments
        self.status = "firing"
        self.detail = (
            f"DETECTED {chosen.get('movie_title')} — {chosen.get('hall')} "
            f"{chosen.get('time_label')}. Resolving {cfg.total_seats} seats."
        )
        target, payments = await self._build_payload(chosen, cfg, int(movie["id"]))
        try:
            await self.group.start(target, payments)
        except GroupPlanError as exc:
            self.status = "error"
            self.detail = f"Detected, but the booking would not start: {exc}"
            return
        self.status = "handed_off"
        self.detail = (
            "Target found and handed off to the group runner. "
            "Watch the Live run panel for the four sessions."
        )

    async def _build_payload(
        self, show: dict, cfg: SnipeConfig, movie_id: int
    ) -> tuple[dict, list[dict]]:
        seat_types = show.get("seat_types") or []
        seat_type = seat_types[0] if seat_types else {}
        seat_type_id = int(seat_type.get("id") or 0)

        seat_catalog = await self.catalog.seats(cfg.location_id, int(show.get("program_id") or 0))
        labels = compute_seat_plan(
            seat_catalog,
            seat_type_id,
            list(cfg.primary_rows),
            cfg.fill_row,
            cfg.total_seats,
            cfg.trim_last,
        )
        chunks = chunk_labels_into(labels, cfg.num_payments)
        if len(chunks) != len(cfg.attendees):
            raise GroupPlanError(
                f"Seat plan split into {len(chunks)} payments but "
                f"{len(cfg.attendees)} attendees were configured."
            )

        target = {
            "location_id": cfg.location_id,
            "location_name": cfg.location_name,
            "show_date": cfg.show_date,
            "movie_id": int(show.get("movie_id") or movie_id),
            "movie_title": str(show.get("movie_title") or cfg.target_movie),
            "program_id": int(show.get("program_id") or 0),
            "screen_id": int(show.get("screen_id") or cfg.hall_id),
            "hall_name": str(show.get("hall") or f"Hall {cfg.hall_id}"),
            "show_time": str(show.get("time") or ""),
            "seat_type_id": seat_type_id,
            "seat_type_name": str(seat_type.get("title") or "Premium"),
            "unit_price": int(seat_type.get("price") or 0),
        }
        payments = [
            {
                "name": attendee.name,
                "bkash_number": attendee.bkash,
                "seats": chunk,
            }
            for attendee, chunk in zip(cfg.attendees, chunks)
        ]
        return target, payments

    def _pick_show(self, shows: list[dict], cfg: SnipeConfig) -> Optional[dict]:
        start = parse_show_time(cfg.time_start)
        end = parse_show_time(cfg.time_end)
        midpoint = (
            start.hour * 60 + start.minute + end.hour * 60 + end.minute
        ) // 2
        candidates: list[tuple[int, dict]] = []
        for show in shows:
            if int(show.get("screen_id") or 0) != int(cfg.hall_id):
                continue
            parsed = parse_show_time(str(show.get("time") or ""))
            if not start <= parsed <= end:
                continue
            candidates.append((abs((parsed.hour * 60 + parsed.minute) - midpoint), show))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
