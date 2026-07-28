"""Pure seat-scoring logic.

No network, no I/O, no time. Everything here is a pure function over a SeatMap
so it unit-tests trivially and can be reused by both the HTTP and Playwright
flow implementations.

Scoring model
-------------
A seat is "good" when it is (a) at an optimal distance from the screen and
(b) horizontally centered. We model both as Gaussian peaks:

    row_score = exp(-0.5 * ((row - ideal_row) / row_sigma)^2)
    col_score = exp(-0.5 * ((col - ideal_col) / col_sigma)^2)

ideal_row defaults to ~65% of the way back from the screen (the conventional
"best" cinema row). ideal_col is the horizontal center.

For a block of N contiguous seats we score by the block's *center* column, not
its leftmost seat, so a centered block beats a lopsided one.

Contiguity is physical: an aisle gap or a taken seat breaks a run. Two seats
across an aisle are never selected as one block.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


@dataclass(frozen=True)
class Seat:
    row: int  # 0-indexed; 0 = nearest the screen
    col: int  # 0-indexed
    row_label: str  # display label, e.g. "A"
    col_label: str  # display label, e.g. "1"
    available: bool
    is_aisle: bool = False  # marks aisle/gap cells (never selectable)
    seat_id: Optional[str] = None  # opaque site-specific id; scorer ignores it


@dataclass
class SeatMap:
    n_rows: int
    n_cols: int
    seats: list[Seat]
    # row -> col -> Seat, built once for O(1) lookup
    _grid: dict[int, dict[int, Seat]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for s in self.seats:
            self._grid.setdefault(s.row, {})[s.col] = s

    def at(self, row: int, col: int) -> Optional[Seat]:
        return self._grid.get(row, {}).get(col)

    def row_seats(self, row: int) -> list[Seat]:
        cells = self._grid.get(row, {})
        return [cells[c] for c in sorted(cells)]

    def rows(self) -> list[int]:
        return sorted(self._grid)


class Zone(str, Enum):
    ANY = "any"
    FRONT = "front"
    MIDDLE = "middle"
    BACK = "back"


@dataclass(frozen=True)
class SeatPreference:
    mode: Literal["auto-best", "preference-plan"] = "auto-best"
    rows: Optional[frozenset[str]] = None  # restrict to these row labels
    zone: Zone = Zone.ANY
    prefer_aisle: bool = False  # nudge blocks whose edge touches an aisle gap


def _ideal_row(n_rows: int, ideal_row_ratio: float) -> float:
    if n_rows <= 1:
        return 0.0
    return ideal_row_ratio * (n_rows - 1)


def score_seat(
    seat: Seat,
    n_rows: int,
    n_cols: int,
    ideal_row_ratio: float = 0.65,
) -> float:
    """Score a single seat in [0, 1]. Higher is better."""
    ir = _ideal_row(n_rows, ideal_row_ratio)
    ic = (n_cols - 1) / 2.0
    row_sigma = max(1.0, n_rows / 3.0)
    col_sigma = max(1.0, n_cols / 3.0)
    row_score = math.exp(-0.5 * ((seat.row - ir) / row_sigma) ** 2)
    col_score = math.exp(-0.5 * ((seat.col - ic) / col_sigma) ** 2)
    return 0.5 * row_score + 0.5 * col_score


def _zone_rows(n_rows: int, zone: Zone) -> set[int]:
    if zone is Zone.ANY:
        return set(range(n_rows))
    third = max(1, n_rows // 3)
    if zone is Zone.FRONT:
        return set(range(0, third))
    if zone is Zone.MIDDLE:
        return set(range(third, n_rows - third)) or set(range(n_rows))
    return set(range(max(0, n_rows - third), n_rows))  # BACK


def _row_passes(seat_row: int, row_label: str, n_rows: int, pref: SeatPreference) -> bool:
    if seat_row not in _zone_rows(n_rows, pref.zone):
        return False
    if pref.rows and row_label not in pref.rows:
        return False
    return True


def _block_score(
    row: int,
    center_col: float,
    n_rows: int,
    n_cols: int,
    ideal_row_ratio: float,
    touches_aisle: bool,
    prefer_aisle: bool,
) -> float:
    ir = _ideal_row(n_rows, ideal_row_ratio)
    ic = (n_cols - 1) / 2.0
    row_sigma = max(1.0, n_rows / 3.0)
    col_sigma = max(1.0, n_cols / 3.0)
    row_score = math.exp(-0.5 * ((row - ir) / row_sigma) ** 2)
    col_score = math.exp(-0.5 * ((center_col - ic) / col_sigma) ** 2)
    base = 0.5 * row_score + 0.5 * col_score
    if prefer_aisle and touches_aisle:
        base += 0.05  # small nudge; never enough to beat a clearly better block
    return base


def _runs_available(seats_in_row: list[Seat]) -> list[list[Seat]]:
    """Split an ordered row into maximal runs of physically-adjacent available
    seats.

    A run breaks on: an aisle cell, a taken/unavailable seat, OR a gap in
    column positions (the real API represents aisles as missing columns, e.g.
    seats at col 6 then col 15 — that 9-col gap is an aisle, not contiguous).
    """
    runs: list[list[Seat]] = []
    current: list[Seat] = []
    prev_col: Optional[int] = None
    for s in seats_in_row:
        selectable = (not s.is_aisle) and s.available
        adjacent = (prev_col is None) or (s.col == prev_col + 1)
        if selectable and adjacent:
            current.append(s)
            prev_col = s.col
        else:
            if current:
                runs.append(current)
                current = []
            if selectable:
                current.append(s)
                prev_col = s.col
            else:
                prev_col = None
    if current:
        runs.append(current)
    return runs


def _touches_aisle(block: list[Seat], seat_map: SeatMap) -> bool:
    """True if the block's left or right edge is adjacent to an aisle gap."""
    first, last = block[0], block[-1]
    left = seat_map.at(first.row, first.col - 1)
    right = seat_map.at(last.row, last.col + 1)
    return bool(left and left.is_aisle) or bool(right and right.is_aisle)


def find_best_block(
    seat_map: SeatMap,
    n: int,
    preference: Optional[SeatPreference] = None,
    ideal_row_ratio: float = 0.65,
) -> Optional[list[Seat]]:
    """Return the best N contiguous available seats, or None if impossible.

    Ties are broken toward the smaller row index (closer to screen) and then
    smaller column, so the result is deterministic.
    """
    if n <= 0:
        raise ValueError("n must be >= 1")
    pref = preference or SeatPreference()

    best_block: Optional[list[Seat]] = None
    best_score = -math.inf

    for row in seat_map.rows():
        sample = next(iter(seat_map._grid[row].values()))
        if not _row_passes(row, sample.row_label, seat_map.n_rows, pref):
            continue
        for run in _runs_available(seat_map.row_seats(row)):
            if len(run) < n:
                continue
            for start in range(0, len(run) - n + 1):
                block = run[start : start + n]
                center_col = (block[0].col + block[-1].col) / 2.0
                score = _block_score(
                    row=row,
                    center_col=center_col,
                    n_rows=seat_map.n_rows,
                    n_cols=seat_map.n_cols,
                    ideal_row_ratio=ideal_row_ratio,
                    touches_aisle=_touches_aisle(block, seat_map),
                    prefer_aisle=pref.prefer_aisle,
                )
                if score > best_score + 1e-12:
                    best_score = score
                    best_block = block
    return best_block


def pick(
    seat_map: SeatMap,
    n: int,
    preference: Optional[SeatPreference] = None,
) -> Optional[list[Seat]]:
    """Convenience alias for find_best_block (the selection entry point)."""
    return find_best_block(seat_map, n, preference)


def seat_view(seat_map: SeatMap) -> dict:
    """Render a SeatMap as a physical-column grid for the interactive UI.

    Emits one cell per column index (0..n_cols-1) for each row, so aisle gaps
    (missing column positions) appear as 'gap' spacer cells and rows of
    different widths line up geometrically across rows. Each seat cell carries a
    status the frontend color-codes; gap cells render as empty spacers.
    """
    rows_out: list[dict] = []
    for r in seat_map.rows():
        cells = seat_map._grid.get(r, {})
        if not cells:
            continue
        row_label = next(iter(cells.values())).row_label
        row_cells: list[dict] = []
        for c in range(seat_map.n_cols):
            s = cells.get(c)
            if s is None:
                row_cells.append(
                    {"id": None, "row": row_label, "col": None, "cidx": c, "status": "gap"}
                )
            else:
                row_cells.append(
                    {
                        "id": s.seat_id or f"{s.row_label}{s.col_label}",
                        "row": s.row_label,
                        "col": s.col_label,
                        "cidx": c,
                        "status": (
                            "aisle"
                            if s.is_aisle
                            else ("taken" if not s.available else "available")
                        ),
                    }
                )
        rows_out.append({"label": row_label, "cells": row_cells})
    return {"n_rows": seat_map.n_rows, "n_cols": seat_map.n_cols, "rows": rows_out}


def seats_by_ids(seat_map: SeatMap, ids: list[str]) -> Optional[list[Seat]]:
    """Resolve a list of seat ids to Seat objects. None if any id is missing."""
    wanted = set(ids)
    found = [s for s in seat_map.seats if s.seat_id in wanted]
    if len(found) != len(wanted):
        return None
    return found


def seatmap_from_view(view: dict) -> SeatMap:
    """Inverse of seat_view: rebuild a SeatMap from a grid view.

    Gap cells (status == 'gap' or no id) contribute no seat. Lets the scorer
    pick against a real map the UI received as a view (e.g. live_seats.json).
    """
    seats: list[Seat] = []
    for row_idx, row in enumerate(view.get("rows", [])):
        for cell in row.get("cells", []):
            if cell.get("status") == "gap" or cell.get("id") is None:
                continue
            cidx = int(cell.get("cidx", 0))
            seats.append(
                Seat(
                    row=row_idx,
                    col=cidx,
                    row_label=cell.get("row") or str(row_idx),
                    col_label=cell.get("col") or str(cidx + 1),
                    available=(cell.get("status") == "available"),
                    is_aisle=(cell.get("status") == "aisle"),
                    seat_id=cell.get("id"),
                )
            )
    return SeatMap(
        n_rows=view.get("n_rows", 0), n_cols=view.get("n_cols", 0), seats=seats
    )
