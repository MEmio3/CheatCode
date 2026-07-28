"""Pure planning rules for the focused Hall 6 group booking.

This module deliberately has no network, browser, FastAPI, or persistence
dependency.  It owns the fixed event target and turns a live Cineplex seat map
into small, physically-contiguous payment chunks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from typing import Iterable, Sequence

from .seats.scorer import Seat, SeatMap


TARGET_MOVIE = "Spider-Man: Brand New Day"
TARGET_DATE = "2026-08-01"
TARGET_LOCATION_ID = 1
TARGET_LOCATION = "Bashundhara Shopping Mall"
TARGET_HALL_ID = 6
TARGET_HALL = "Hall 6"
TARGET_TIME_START = time(16, 0)
TARGET_TIME_END = time(18, 0)
TARGET_ROWS = ("E", "F")
SITE_TRANSACTION_CAP = 10


class GroupPlanError(ValueError):
    """The requested show or seat arrangement cannot be safely planned."""


@dataclass(frozen=True)
class ShowChoice:
    movie_id: int
    movie_title: str
    program_id: int
    screen_id: int
    show_time: str
    seat_type_id: int | None = None
    seat_type_name: str = "Premium"
    unit_price: int | None = None


@dataclass(frozen=True)
class SeatChunk:
    index: int
    row: str
    seats: tuple[Seat, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(f"{seat.row_label}{seat.col_label}" for seat in self.seats)

    @property
    def seat_ids(self) -> tuple[str, ...]:
        return tuple(str(seat.seat_id) for seat in self.seats)


def normalize_title(value: str) -> str:
    """Normalize punctuation/spacing so movie-title matching survives UI churn."""
    value = value.casefold().replace("–", "-").replace("—", "-")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def movie_matches(value: str, target: str = TARGET_MOVIE) -> bool:
    wanted = normalize_title(target)
    actual = normalize_title(value)
    return wanted in actual or actual in wanted


def parse_show_time(value: str) -> time:
    """Parse Cineplex's ``HH:MM[:SS]`` value."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", value or "")
    if not match:
        raise GroupPlanError(f"Unrecognized Cineplex show time: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise GroupPlanError(f"Invalid Cineplex show time: {value!r}")
    return time(hour, minute)


def display_show_time(value: str) -> str:
    parsed = parse_show_time(value)
    hour = parsed.hour % 12 or 12
    suffix = "AM" if parsed.hour < 12 else "PM"
    return f"{hour}:{parsed.minute:02d} {suffix}"


def choose_hall_show(
    shows: Iterable[dict],
    *,
    start: time = TARGET_TIME_START,
    end: time = TARGET_TIME_END,
    hall_id: int = TARGET_HALL_ID,
) -> ShowChoice:
    """Choose the Hall 6 show inside the requested window, closest to 5 PM."""
    candidates: list[tuple[int, ShowChoice]] = []
    midpoint = (start.hour * 60 + start.minute + end.hour * 60 + end.minute) // 2

    for show in shows:
        screen_id = int(show.get("screenID") or show.get("screenId") or 0)
        if screen_id != hall_id:
            continue
        movie_id = int(show.get("movieId") or show.get("movieID") or 0)
        movie_title = str(show.get("movieTitle") or "")
        for slot in show.get("showTimes") or []:
            raw_time = str(slot.get("showTime") or "")
            parsed = parse_show_time(raw_time)
            if not start <= parsed <= end:
                continue
            prices = slot.get("seatPrices") or []
            premium = next(
                (
                    price
                    for price in prices
                    if "premium" in str(
                        price.get("seatTypeName")
                        or price.get("seatTypeTitle")
                        or price.get("seatType")
                        or ""
                    ).casefold()
                ),
                prices[0] if prices else {},
            )
            unit_price = premium.get("unitPrice")
            choice = ShowChoice(
                movie_id=movie_id,
                movie_title=movie_title,
                program_id=int(slot.get("programId") or 0),
                screen_id=screen_id,
                show_time=raw_time,
                seat_type_id=(
                    int(
                        premium.get("seatTypeId")
                        or premium.get("seatTypeID")
                        or premium.get("classId")
                    )
                    if (
                        premium.get("seatTypeId")
                        or premium.get("seatTypeID")
                        or premium.get("classId")
                    )
                    is not None
                    else None
                ),
                seat_type_name=str(
                    premium.get("seatTypeName")
                    or premium.get("seatTypeTitle")
                    or "Premium"
                ),
                unit_price=int(unit_price) if unit_price is not None else None,
            )
            minute_value = parsed.hour * 60 + parsed.minute
            candidates.append((abs(minute_value - midpoint), choice))

    if not candidates:
        raise GroupPlanError(
            "No Hall 6 show is published between 4:00 PM and 6:00 PM for the target date."
        )
    candidates.sort(key=lambda item: (item[0], parse_show_time(item[1].show_time)))
    return candidates[0][1]


def plan_full_rows(
    seat_map: SeatMap,
    *,
    rows: Sequence[str] = TARGET_ROWS,
    cap: int = SITE_TRANSACTION_CAP,
) -> list[SeatChunk]:
    """Require every real seat in E/F and split each row without crossing rows.

    Hall 6's captured layout has 17 seats in E and 17 in F, so the natural plan
    is 10 + 7 + 10 + 7.  Keeping chunks inside one row makes each payment's seat
    assignment obvious and prevents a partial transaction from scattering the
    group.
    """
    if cap <= 0:
        raise GroupPlanError("Transaction cap must be positive.")

    by_label: dict[str, list[Seat]] = {}
    for row in rows:
        row_seats = sorted(
            (seat for seat in seat_map.seats if seat.row_label.casefold() == row.casefold()),
            key=lambda seat: seat.col,
        )
        if not row_seats:
            raise GroupPlanError(f"Row {row} is missing from the live Hall 6 seat map.")
        unavailable = [
            f"{seat.row_label}{seat.col_label}"
            for seat in row_seats
            if not seat.available or seat.is_aisle
        ]
        if unavailable:
            preview = ", ".join(unavailable[:8])
            extra = f" (+{len(unavailable) - 8} more)" if len(unavailable) > 8 else ""
            raise GroupPlanError(
                f"Rows E and F are no longer fully available. Unavailable: {preview}{extra}."
            )
        by_label[row] = row_seats

    chunks: list[SeatChunk] = []
    for row in rows:
        row_seats = by_label[row]
        for offset in range(0, len(row_seats), cap):
            seats = tuple(row_seats[offset : offset + cap])
            chunks.append(SeatChunk(index=len(chunks) + 1, row=row, seats=seats))
    return chunks


def validate_names(names: Sequence[str], required: int) -> list[str]:
    cleaned = [re.sub(r"\s+", " ", name).strip() for name in names]
    if any(not name for name in cleaned):
        raise GroupPlanError("Every payment session needs a real attendee name.")
    if len(cleaned) != required:
        raise GroupPlanError(
            f"This seat plan needs exactly {required} attendee names, got {len(cleaned)}."
        )
    if len({name.casefold() for name in cleaned}) != len(cleaned):
        raise GroupPlanError("Use a different real attendee name for each payment session.")
    return cleaned


def validate_bkash_number(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "", value)
    if normalized.startswith("+88"):
        normalized = normalized[3:]
    elif normalized.startswith("88") and len(normalized) == 13:
        normalized = normalized[2:]
    if not re.fullmatch(r"01[3-9]\d{8}", normalized):
        raise GroupPlanError("Enter a valid 11-digit Bangladesh bKash number.")
    return normalized


def mask_phone(value: str) -> str:
    return f"{value[:3]} ••• ••• {value[-3:]}"
