"""Auto-sniper: watch the public schedule until a target movie becomes bookable
on a specific date/hall/time window, then fire a four-session group booking for
a live-computed block of seats.

Seat plan is computed from the live layout, not a fixed label list, so it
adapts when the target drops. ``compute_seat_plan`` picks the most cohesive
arrangement for the group: a single unbroken block when one fits, otherwise
balanced blocks across adjacent rows (6+5, then 7+4...) so nobody is isolated.
``tolerance`` is the isolation floor (a fragment smaller than it counts its
seats as isolated and is avoided); ``force`` bypasses cohesion and just grabs
the first ``total_seats`` available. The assembled labels are split into <=10
chunks and zipped one-to-one with the attendees.

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
from datetime import date
from typing import Any, Optional

from .config_store import CredentialStore, telegram_env_credentials
from .group import GroupPlanError, movie_matches, parse_show_time, validate_bkash_number, validate_names

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
    target_movie: str = ""
    location_id: int = 0
    location_name: str = ""
    all_locations: bool = False
    hall_ids: list[int] = field(default_factory=list)
    show_date: str = ""
    time_start: str = ""  # Empty means any show time.
    time_end: str = ""
    poll_seconds: int = 75
    total_seats: int = 1
    primary_rows: list[str] = field(default_factory=list)
    fill_row: str = ""
    tolerance: int = 3
    force: bool = False
    num_payments: int = 1
    allow_duplicate_identity: bool = False
    attendees: list[SnipeAttendee] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_movie": self.target_movie,
            "location_id": self.location_id,
            "location_name": self.location_name,
            "all_locations": self.all_locations,
            "hall_ids": list(self.hall_ids),
            "show_date": self.show_date,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "poll_seconds": self.poll_seconds,
            "total_seats": self.total_seats,
            "primary_rows": list(self.primary_rows),
            "fill_row": self.fill_row,
            "tolerance": self.tolerance,
            "force": self.force,
            "num_payments": self.num_payments,
            "allow_duplicate_identity": self.allow_duplicate_identity,
            "attendees": [{"name": a.name, "bkash": a.bkash} for a in self.attendees],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SnipeConfig":
        return cls(
            target_movie=str(d.get("target_movie") or ""),
            location_id=int(d.get("location_id") or 0),
            location_name=str(d.get("location_name") or ""),
            all_locations=bool(d.get("all_locations", False)),
            hall_ids=[int(value) for value in (d.get("hall_ids") or ([d.get("hall_id")] if d.get("hall_id") else []))],
            show_date=str(d.get("show_date") or ""),
            time_start=str(d.get("time_start") or ""),
            time_end=str(d.get("time_end") or ""),
            poll_seconds=int(d.get("poll_seconds") or 75),
            total_seats=int(d.get("total_seats") or 1),
            primary_rows=[str(r) for r in (d.get("primary_rows") or [])],
            fill_row=str(d.get("fill_row") or ""),
            tolerance=int(d.get("tolerance") or 3),
            force=bool(d.get("force", False)),
            num_payments=int(d.get("num_payments") or 1),
            allow_duplicate_identity=bool(d.get("allow_duplicate_identity", False)),
            attendees=[
                SnipeAttendee(name=str(a.get("name") or ""), bkash=str(a.get("bkash") or ""))
                for a in (d.get("attendees") or [])
            ],
        )

    def validate(self) -> None:
        if not self.target_movie.strip():
            raise GroupPlanError("Pick a target movie to watch for.")
        if not self.all_locations and self.location_id <= 0:
            raise GroupPlanError("A location is required, or enable 'check all locations'.")
        try:
            date.fromisoformat(self.show_date)
        except ValueError as exc:
            raise GroupPlanError("Show date must use YYYY-MM-DD.") from exc
        if len(set(self.hall_ids)) != len(self.hall_ids) or any(hall <= 0 for hall in self.hall_ids):
            raise GroupPlanError("Hall preferences must be distinct positive numbers.")
        if bool(self.time_start) != bool(self.time_end):
            raise GroupPlanError("Set both show-time limits or leave both empty.")
        if self.time_start and parse_show_time(self.time_start) >= parse_show_time(self.time_end):
            raise GroupPlanError("Show-time start must be earlier than the end.")
        if not (1 <= self.total_seats <= 40):
            raise GroupPlanError("Total seats must be between 1 and 40.")
        if not (1 <= self.tolerance <= 6):
            raise GroupPlanError("Tolerance must be between 1 and 6.")
        self.primary_rows = [row.strip().upper() for row in self.primary_rows if row.strip()]
        self.fill_row = self.fill_row.strip().upper()
        if len(set(self.primary_rows)) != len(self.primary_rows) or (self.fill_row and self.fill_row in self.primary_rows):
            raise GroupPlanError("Rows must be unique; the fill row cannot also be a primary row.")
        if self.fill_row and not self.primary_rows:
            raise GroupPlanError("Choose primary rows before choosing a fill row.")
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
        validate_names(
            [attendee.name for attendee in self.attendees],
            self.num_payments,
            allow_duplicates=self.allow_duplicate_identity,
        )
        phones = [validate_bkash_number(attendee.bkash) for attendee in self.attendees]
        if not self.allow_duplicate_identity and len(set(phones)) != len(phones):
            raise GroupPlanError("Use a different bKash number for each attendee.")


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


@dataclass(frozen=True)
class _Block:
    row_index: int
    row_label: str
    cmin: int
    cmax: int
    size: int
    labels: tuple[str, ...]


def _row_runs(cells: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a row's available cells (already sorted by ``cidx``) into maximal
    physically-contiguous runs.

    Contiguity is physical, not just "available": a taken seat OR an aisle gap
    (a jump in ``cidx``) breaks a run, so two seats across an aisle are never
    treated as one block.
    """
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev: int | None = None
    for cell in cells:
        cidx = int(cell.get("cidx") or 0)
        if prev is not None and cidx != prev + 1:
            runs.append(current)
            current = []
        current.append(cell)
        prev = cidx
    if current:
        runs.append(current)
    return runs


def _available_rows(
    seat_type: dict[str, Any],
) -> list[tuple[int, str, list[list[dict[str, Any]]]]]:
    """Rows with at least one available seat -> ``(row_index, label, runs)``."""
    out: list[tuple[int, str, list[list[dict[str, Any]]]]] = []
    for ridx, row in enumerate(seat_type.get("rows") or []):
        label = str(row.get("label") or "").upper()
        cells = sorted(
            (
                c
                for c in (row.get("cells") or [])
                if c.get("status") == "available" and c.get("label")
            ),
            key=lambda c: int(c.get("cidx") or 0),
        )
        runs = _row_runs(cells)
        if runs:
            out.append((ridx, label, runs))
    return out


def _best_window(run: list[dict[str, Any]], size: int, n_cols: int) -> list[dict[str, Any]]:
    """The window of ``size`` consecutive seats in ``run`` whose center is
    closest to the hall's horizontal center."""
    target = (max(1, n_cols) - 1) / 2.0
    best_off, best_d = 0, float("inf")
    for off in range(0, len(run) - size + 1):
        window = run[off : off + size]
        center = (int(window[0]["cidx"]) + int(window[-1]["cidx"])) / 2.0
        d = abs(center - target)
        if d < best_d:
            best_d, best_off = d, off
    return run[best_off : best_off + size]


def _make_block(
    run: list[dict[str, Any]], row_index: int, row_label: str, size: int, n_cols: int
) -> _Block:
    window = _best_window(run, size, n_cols)
    cidxs = [int(c["cidx"]) for c in window]
    return _Block(
        row_index=row_index,
        row_label=row_label,
        cmin=min(cidxs),
        cmax=max(cidxs),
        size=size,
        labels=tuple(str(c["label"]) for c in window),
    )


def _selection_cost(
    blocks: list[_Block], tolerance: int, preferred: set[str], n_cols: int
) -> tuple:
    """Lexicographic cost of a complete selection (every element: lower better).

    Order: fewest isolated people, then fewest fragments, then tightest row
    span, then tightest column span, then best-centered, then preferred rows.
    """
    isolated = sum(b.size for b in blocks if b.size < tolerance)
    frags = len(blocks)
    row_span = max(b.row_index for b in blocks) - min(b.row_index for b in blocks)
    col_extent = max(b.cmax for b in blocks) - min(b.cmin for b in blocks)
    target = (max(1, n_cols) - 1) / 2.0
    center = sum(((b.cmin + b.cmax) / 2.0 - target) ** 2 for b in blocks)
    non_pref = sum(1 for b in blocks if b.row_label not in preferred)
    return (isolated, frags, row_span, col_extent, center, non_pref)


def _cohesive_select(
    rows: list[tuple[int, str, list[list[dict[str, Any]]]]],
    total: int,
    tolerance: int,
    preferred: list[str],
    fill_row: str,
    n_cols: int,
) -> Optional[list[_Block]]:
    """Search contiguous-block combinations summing to ``total``; return the
    min-cost selection, or ``None`` if no combination reaches ``total``.

    A row with several runs (e.g. a middle-taken row) may contribute one block
    per run, but fragment count is penalized so that only happens when the
    alternative is leaving seats unfilled.
    """
    # Flatten runs into independent sources, biggest first so low-fragment
    # solutions surface early (improves the prefix prune below).
    sources: list[tuple[int, str, list[dict[str, Any]]]] = []
    for ridx, label, runs in rows:
        for run in runs:
            sources.append((ridx, label, run))
    sources.sort(key=lambda s: len(s[2]), reverse=True)

    # Precompute the best-centered block for each (source, size) once.
    candidates: list[dict[int, _Block]] = []
    for ridx, label, run in sources:
        candidates.append({s: _make_block(run, ridx, label, s, n_cols) for s in range(1, len(run) + 1)})

    preferred_set = set(preferred)
    if fill_row:
        preferred_set.add(fill_row)
    max_frags = min(8, total)

    best: dict[str, Any] = {"cost": None, "blocks": None}

    def recurse(i: int, remaining: int, chosen: list[_Block], iso: int) -> None:
        if remaining == 0:
            cost = _selection_cost(chosen, tolerance, preferred_set, n_cols)
            if best["cost"] is None or cost < best["cost"]:
                best["cost"] = cost
                best["blocks"] = list(chosen)
            return
        if i >= len(sources) or len(chosen) >= max_frags:
            return
        # Prefix prune: isolated-people and fragment-count only grow as we add
        # blocks, so a partial already worse than the best complete selection
        # on those two dominant axes cannot be improved.
        if best["cost"] is not None:
            if (iso, len(chosen)) > (best["cost"][0], best["cost"][1]):
                return
        # Option A: skip this run.
        recurse(i + 1, remaining, chosen, iso)
        # Option B: take a block of size s from this run (largest first).
        max_s = min(len(sources[i][2]), remaining)
        for s in range(max_s, 0, -1):
            chosen.append(candidates[i][s])
            recurse(i + 1, remaining - s, chosen, iso + (s if s < tolerance else 0))
            chosen.pop()

    recurse(0, total, [], 0)
    return best["blocks"]


def _force_fill(
    rows: list[tuple[int, str, list[list[dict[str, Any]]]]],
    primary_rows: list[str],
    fill_row: str,
    total: int,
) -> list[str]:
    """Cohesion-agnostic fill: the first ``total`` available seats, scanning
    preferred rows first (then the fill row, then the rest) left-to-right."""
    pref_order = list(dict.fromkeys(primary_rows))
    if fill_row and fill_row not in pref_order:
        pref_order.append(fill_row)
    by_label = {r[1]: r for r in rows}
    ordered = [by_label[lbl] for lbl in pref_order if lbl in by_label]
    ordered += [r for r in rows if r[1] not in set(pref_order)]
    picked: list[str] = []
    for _, _, runs in ordered:
        for run in runs:
            for cell in run:
                picked.append(str(cell["label"]))
                if len(picked) == total:
                    return picked
    return picked


def compute_seat_plan(
    seat_catalog: dict[str, Any],
    seat_type_id: int,
    primary_rows: list[str],
    fill_row: str,
    total: int,
    *,
    tolerance: int = 3,
    force: bool = False,
) -> list[str]:
    """Resolve ``total`` seat labels from a live seat map as one cohesive group.

    Prefers a single unbroken block. When the group must split, it splits into
    balanced blocks across adjacent rows so nobody is isolated: a fragment
    smaller than ``tolerance`` counts its seats as isolated and is avoided
    (searching harder / expanding rows first). ``force`` bypasses cohesion and
    just grabs the first ``total`` available seats.

    Labels come back grouped by row in physical order, so the caller's
    payment-chunking keeps each transaction physically contiguous.
    """
    if total <= 0:
        raise GroupPlanError("Total seats must be positive.")
    seat_type = next(
        (st for st in (seat_catalog.get("seat_types") or [])
         if int(st.get("id") or 0) == int(seat_type_id)),
        None,
    )
    if seat_type is None:
        raise GroupPlanError("The chosen seat class is not in the live layout.")

    rows = _available_rows(seat_type)
    if not rows:
        raise GroupPlanError("No available seats remain in the live layout.")

    pref = [str(r).strip().upper() for r in primary_rows if str(r).strip()]
    fill = (fill_row or "").strip().upper()

    if force:
        labels = _force_fill(rows, pref, fill, total)
    else:
        n_cols = int(seat_type.get("n_cols") or 0)
        blocks = _cohesive_select(rows, total, tolerance, pref, fill, n_cols)
        if blocks is None:
            # No cohesive combination reached `total`; fall back to any fill
            # rather than abort the whole run.
            labels = _force_fill(rows, pref, fill, total)
        else:
            blocks.sort(key=lambda b: (b.row_index, b.cmin))
            labels = [label for b in blocks for label in b.labels]

    if len(labels) != total:
        raise GroupPlanError(
            f"Could not assemble {total} seats; only {len(labels)} available in the live layout."
        )
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
        self._last_report_at = None

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

    async def test_config(self, cfg: SnipeConfig) -> dict[str, Any]:
        """Dry-run the watcher against the live schedule WITHOUT firing."""
        cfg.validate()
        result: dict[str, Any] = {
            "match": False, "target_movie": cfg.target_movie,
            "show_date": cfg.show_date, "location": cfg.location_name, "detail": "",
        }
        if cfg.all_locations:
            try:
                locations = await self.catalog.locations()
            except Exception as exc:
                result["detail"] = f"Could not load locations: {exc}"
                return result
            search = [
                {"id": int(loc.get("id") or 0), "title": str(loc.get("title") or "")}
                for loc in locations
                if int(loc.get("id") or 0) > 0
            ]
            for loc in search:
                match = await self._find_match(cfg, loc["id"], loc["title"])
                if match:
                    chosen, _ = match
                    result["match"] = True
                    result["location"] = loc["title"]
                    result["show"] = chosen
                    result["detail"] = (
                        f"MATCH at {loc['title']}: {chosen.get('hall')} "
                        f"{chosen.get('time_label')} (program {chosen.get('program_id')})"
                    )
                    return result
            result["detail"] = (
                f"No match for '{cfg.target_movie}' on {cfg.show_date} across "
                f"{len(search)} location(s)."
            )
            return result
        try:
            dates = await self.catalog.dates(cfg.location_id)
        except Exception as exc:
            result["detail"] = f"Catalog error: {exc}"
            return result
        day = next((d for d in dates if str(d.get("date") or "") == cfg.show_date), None)
        if day is None:
            result["detail"] = f"{cfg.show_date} is not published at {cfg.location_name} yet."
            result["published_dates"] = [str(d.get("date")) for d in dates]
            return result
        movie = next((m for m in (day.get("movies") or [])
                       if movie_matches(str(m.get("title") or ""), cfg.target_movie)), None)
        if movie is None or not int(movie.get("id") or 0):
            result["detail"] = f"'{cfg.target_movie}' is not listed on {cfg.show_date}."
            result["available_movies"] = [str(m.get("title")) for m in (day.get("movies") or [])]
            return result
        result["movie_id"] = int(movie["id"])
        result["movie_title"] = str(movie.get("title") or cfg.target_movie)
        shows = await self.catalog.shows(cfg.location_id, int(movie["id"]), cfg.show_date)
        chosen = self._pick_show(shows, cfg)
        if chosen is None:
            result["detail"] = "Listed, but no preferred hall/time show available."
            result["shows"] = [{"hall": s.get("hall"), "time": s.get("time_label")} for s in shows]
            return result
        result["match"] = True
        result["show"] = chosen
        result["detail"] = f"MATCH: {chosen.get('hall')} {chosen.get('time_label')} (program {chosen.get('program_id')})"
        return result

    async def start(self, config: SnipeConfig) -> None:
        if self.busy:
            raise GroupPlanError("The sniper is already running.")
        config.validate()
        self.config = config
        save_config(config)
        _write_active()
        self.status = "watching"
        where = "all locations" if config.all_locations else config.location_name
        self.detail = (
            f"Watching for '{config.target_movie}' on {config.show_date} at "
            f"{where} "
            f"(halls {', '.join(map(str, config.hall_ids)) or 'any'}, "
            f"times {config.time_start or 'any'}-{config.time_end or 'any'}); "
            f"{config.total_seats} seats."
        )
        self.last_poll_at = None
        self._last_report_at = None
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
                    await self._report(cfg, self.detail)
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

        if cfg.all_locations:
            self.detail = f"Scanning every location for '{cfg.target_movie}' on {cfg.show_date}..."
            try:
                locations = await self.catalog.locations()
            except Exception as exc:
                self.detail = f"Could not load locations: {exc}. Next poll in {cfg.poll_seconds}s."
                await self._report(cfg, self.detail)
                return
            search = [
                {"id": int(loc.get("id") or 0), "title": str(loc.get("title") or "")}
                for loc in locations
                if int(loc.get("id") or 0) > 0
            ]
        else:
            self.detail = f"Polling schedule for '{cfg.target_movie}' on {cfg.show_date}..."
            search = [{"id": cfg.location_id, "title": cfg.location_name}]

        for loc in search:
            match = await self._find_match(cfg, loc["id"], loc["title"])
            if match:
                chosen, movie_id = match
                await self._fire(cfg, chosen, movie_id, loc["id"], loc["title"])
                return

        if cfg.all_locations:
            self.detail = (
                f"No matching show for '{cfg.target_movie}' on {cfg.show_date} across "
                f"{len(search)} location(s). Next poll in {cfg.poll_seconds}s."
            )
        else:
            self.detail = (
                f"'{cfg.target_movie}' is not bookable on {cfg.show_date} at "
                f"{cfg.location_name} yet. Next poll in {cfg.poll_seconds}s."
            )
        await self._report(cfg, self.detail)

    async def _find_match(
        self, cfg: SnipeConfig, location_id: int, location_name: str
    ) -> Optional[tuple[dict, int]]:
        """Search one location for the target show. Returns (chosen_show,
        movie_id) on a match, else None. Swallowing per-location misses keeps
        an all-locations scan quiet until something actually matches."""
        try:
            dates = await self.catalog.dates(location_id)
        except Exception as exc:
            log.debug("sniper: %s catalog error: %s", location_name, exc)
            return None
        target_day = next((d for d in dates if str(d.get("date") or "") == cfg.show_date), None)
        if target_day is None:
            return None
        movie = next(
            (
                m for m in (target_day.get("movies") or [])
                if movie_matches(str(m.get("title") or ""), cfg.target_movie)
            ),
            None,
        )
        if movie is None or not int(movie.get("id") or 0):
            return None
        shows = await self.catalog.shows(location_id, int(movie["id"]), cfg.show_date)
        chosen = self._pick_show(shows, cfg)
        if chosen is None:
            return None
        return chosen, int(movie["id"])

    async def _fire(
        self, cfg: SnipeConfig, chosen: dict, movie_id: int,
        location_id: int, location_name: str,
    ) -> None:
        self.status = "firing"
        self.detail = (
            f"DETECTED {chosen.get('movie_title')} — {chosen.get('hall')} "
            f"{chosen.get('time_label')} at {location_name}. Resolving {cfg.total_seats} seats."
        )
        match_msg = (
            f"🎯 MATCH FOUND — BOOKING NOW\n"
            f"Movie: {chosen.get('movie_title')}\n"
            f"Hall: {chosen.get('hall')} | Time: {chosen.get('time_label')}\n"
            f"Date: {cfg.show_date} | Location: {location_name}\n"
            f"Seats: {cfg.total_seats} across {cfg.num_payments} payment(s)"
        )
        await self._report(cfg, match_msg, force=True)
        required = ("program_id", "screen_id", "time")
        missing_fields = [f for f in required if not chosen.get(f)]
        if missing_fields:
            self.status = "error"
            self.detail = f"Show data incomplete (missing {', '.join(missing_fields)}). Cannot book."
            await self._report(cfg, self.detail, force=True)
            return
        target, payments = await self._build_payload(
            chosen, cfg, movie_id, location_id, location_name
        )
        try:
            await self.group.start(
                target,
                payments,
                allow_duplicate_identity=cfg.allow_duplicate_identity,
            )
        except GroupPlanError as exc:
            self.status = "error"
            self.detail = f"Detected, but the booking would not start: {exc}"
            await self._report(cfg, self.detail, force=True)
            return
        self.status = "handed_off"
        self.detail = (
            "Target found and handed off to the group runner. "
            "Watch the Live run panel for the sessions."
        )
        await self._report(cfg, self.detail, force=True)

    async def _report(self, cfg: SnipeConfig, message: str, *, force: bool = False) -> None:
        if self._last_report_at is not None and not force and time.monotonic() - self._last_report_at < 1_800:
            return
        self._last_report_at = time.monotonic()
        try:
            store = CredentialStore.auto()
            env_token, env_chat_id = telegram_env_credentials()
            token = env_token or store.get("telegram_bot_token")
            chat_id = env_chat_id or store.get("telegram_chat_id")
            if not token or not chat_id:
                return
            from telegram import Bot

            async with Bot(token=token) as bot:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"CineBot watcher: {message}",
                    disable_web_page_preview=True,
                )
        except Exception as exc:
            log.warning("Telegram watcher report failed: %s", exc)

    async def _build_payload(
        self, show: dict, cfg: SnipeConfig, movie_id: int,
        location_id: Optional[int] = None, location_name: Optional[str] = None,
    ) -> tuple[dict, list[dict]]:
        loc_id = location_id if location_id is not None else cfg.location_id
        loc_name = location_name or cfg.location_name
        seat_types = show.get("seat_types") or []
        seat_type = seat_types[0] if seat_types else {}
        seat_type_id = int(seat_type.get("id") or 0)

        seat_catalog = await self.catalog.seats(loc_id, int(show.get("program_id") or 0))
        labels = compute_seat_plan(
            seat_catalog,
            seat_type_id,
            list(cfg.primary_rows),
            cfg.fill_row,
            cfg.total_seats,
            tolerance=cfg.tolerance,
            force=cfg.force,
        )
        chunks = chunk_labels_into(labels, cfg.num_payments)
        if len(chunks) != len(cfg.attendees):
            raise GroupPlanError(
                f"Seat plan split into {len(chunks)} payments but "
                f"{len(cfg.attendees)} attendees were configured."
            )

        target = {
            "location_id": loc_id,
            "location_name": loc_name,
            "show_date": cfg.show_date,
            "movie_id": int(show.get("movie_id") or movie_id),
            "movie_title": str(show.get("movie_title") or cfg.target_movie),
            "program_id": int(show.get("program_id") or 0),
            "screen_id": int(show.get("screen_id") or 0),
            "hall_name": str(show.get("hall") or "Preferred hall"),
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
        start = parse_show_time(cfg.time_start) if cfg.time_start else None
        end = parse_show_time(cfg.time_end) if cfg.time_end else None
        midpoint = ((start.hour * 60 + start.minute + end.hour * 60 + end.minute) // 2) if start and end else 0
        candidates: list[tuple[int, dict]] = []
        for show in shows:
            if cfg.hall_ids and int(show.get("screen_id") or 0) not in cfg.hall_ids:
                continue
            parsed = parse_show_time(str(show.get("time") or ""))
            if start and end and not start <= parsed <= end:
                continue
            candidates.append((abs((parsed.hour * 60 + parsed.minute) - midpoint) if start else 0, show))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
