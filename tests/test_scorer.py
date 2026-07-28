"""Tests for cinebot.seats.scorer — pure selection logic."""
from __future__ import annotations

import math

import pytest

from cinebot.seats.scorer import (
    Seat,
    SeatMap,
    SeatPreference,
    Zone,
    find_best_block,
    pick,
    score_seat,
    _block_score,
    _touches_aisle,
)


def _label_row(r: int) -> str:
    return chr(ord("A") + r)


def make_map(
    n_rows: int,
    n_cols: int,
    *,
    unavailable: set[tuple[int, int]] | None = None,
    aisles: set[tuple[int, int]] | None = None,
) -> SeatMap:
    unavailable = unavailable or set()
    aisles = aisles or set()
    seats: list[Seat] = []
    for r in range(n_rows):
        for c in range(n_cols):
            cell = (r, c)
            seats.append(
                Seat(
                    row=r,
                    col=c,
                    row_label=_label_row(r),
                    col_label=str(c + 1),
                    available=cell not in unavailable and cell not in aisles,
                    is_aisle=cell in aisles,
                    seat_id=f"{_label_row(r)}{c + 1}",
                )
            )
    return SeatMap(n_rows=n_rows, n_cols=n_cols, seats=seats)


# ---- single-seat scoring ------------------------------------------------====


def test_score_seat_peaks_near_ideal_row_and_center_col():
    # 10x10: ideal_row = 0.65*9 = 5.85, ideal_col = 4.5
    sm = make_map(10, 10)
    peak = score_seat(Seat(row=6, col=4, row_label="G", col_label="5", available=True), 10, 10)
    far = score_seat(Seat(row=0, col=0, row_label="A", col_label="1", available=True), 10, 10)
    assert peak > far
    assert 0.0 <= far < peak <= 1.0


def test_score_seat_is_in_unit_interval():
    sm_seat = Seat(row=2, col=3, row_label="C", col_label="4", available=True)
    s = score_seat(sm_seat, 8, 8)
    assert 0.0 <= s <= 1.0


# ---- block selection ------------------------------------------------------====


def test_best_block_is_centered_in_optimal_row():
    sm = make_map(10, 10)
    block = find_best_block(sm, 2)
    assert block is not None
    # ideal row 6 ("G"), centered block cols 4,5 ("5","6")
    assert [s.row for s in block] == [6, 6]
    assert [s.col_label for s in block] == ["5", "6"]


def test_best_block_single_seat():
    sm = make_map(10, 10)
    block = find_best_block(sm, 1)
    assert block is not None
    assert len(block) == 1
    # single best seat: row 6, col 4 or 5 (both center). Tie-break -> col 4.
    assert (block[0].row, block[0].col) == (6, 4)


def test_block_avoids_taken_seats():
    # block the same column across ALL rows so the scorer can't dodge to a
    # better-centered block in another row; it must avoid the taken column.
    sm = make_map(10, 10, unavailable={(r, 4) for r in range(10)})
    block = find_best_block(sm, 2)
    assert block is not None
    block_cells = [(s.row, s.col) for s in block]
    for r in range(10):
        assert (r, 4) not in block_cells
    cols = [s.col for s in block]
    assert cols[1] - cols[0] == 1  # contiguous
    # with col 4 gone everywhere, best centered block is cols 5,6 in ideal row 6
    assert [s.row for s in block] == [6, 6]
    assert cols == [5, 6]


def test_block_does_not_cross_aisle_gap():
    # structural aisle at col 5 in every row -> no block may include or straddle it
    sm = make_map(10, 10, aisles={(r, 5) for r in range(10)})
    block = find_best_block(sm, 2)
    assert block is not None
    cols = [s.col for s in block]
    # contiguous AND contains no aisle cell: together these prove it cannot
    # have crossed the col-5 gap (4 -> 6 would require 5)
    assert cols[1] - cols[0] == 1
    assert 5 not in cols
    assert all(c < 5 for c in cols) or all(c > 5 for c in cols)


def test_returns_none_when_not_enough_contiguous():
    # every row only has isolated singles
    sm = make_map(
        3,
        3,
        unavailable={(0, 1), (1, 1), (2, 1)},  # middle col taken everywhere
    )
    assert find_best_block(sm, 2) is None


def test_returns_none_when_preference_row_has_no_space():
    sm = make_map(10, 10)
    pref = SeatPreference(mode="preference-plan", rows=frozenset({"A"}))
    # row A has space, but ask for more than exists by marking most taken
    sm2 = make_map(10, 10, unavailable={(0, c) for c in range(8)})
    assert find_best_block(sm2, 3, pref) is None


def test_invalid_n_raises():
    sm = make_map(5, 5)
    with pytest.raises(ValueError):
        find_best_block(sm, 0)


# ---- preferences ----------------------------------------------------------====


def test_preference_restricts_to_named_rows():
    sm = make_map(10, 10)
    pref = SeatPreference(mode="preference-plan", rows=frozenset({"C", "D"}))
    block = find_best_block(sm, 2, pref)
    assert block is not None
    assert all(s.row_label in {"C", "D"} for s in block)
    # among C (row 2) and D (row 3), D is closer to ideal row 6 -> picks D
    assert all(s.row_label == "D" for s in block)


def test_zone_middle_restricts_rows():
    # 9 rows, third=3 -> MIDDLE = rows 3,4,5
    sm = make_map(9, 10)
    pref = SeatPreference(mode="preference-plan", zone=Zone.MIDDLE)
    block = find_best_block(sm, 2, pref)
    assert block is not None
    assert all(s.row in {3, 4, 5} for s in block)
    # ideal row = 0.65*8 = 5.2 -> within middle, row 5 is closest
    assert all(s.row == 5 for s in block)


def test_prefer_aisle_flips_near_tie():
    # 1 row, cols 0..8. Col 4 taken, col 6 is an aisle gap.
    # ideal_col = 4 -> col 3 and col 5 are equidistant (base score tied).
    # col 5 touches the aisle on its right -> prefer_aisle should flip to col 5.
    sm = make_map(1, 9, unavailable={(0, 4)}, aisles={(0, 6)})

    without = find_best_block(sm, 1, SeatPreference(prefer_aisle=False))
    with_aisle = find_best_block(sm, 1, SeatPreference(prefer_aisle=True))

    assert without is not None and with_aisle is not None
    assert without[0].col == 3  # tie-break -> lower col
    assert with_aisle[0].col == 5  # aisle bonus flips it


def test_block_does_not_cross_missing_col_gap():
    # Real API shape: aisles are MISSING columns (e.g. cols 0-5 then 10-15,
    # no seats at 6-9). A block must not span that gap.
    seats = []
    for r in range(3):
        for c in list(range(0, 6)) + list(range(10, 16)):
            seats.append(
                Seat(
                    row=r, col=c,
                    row_label=chr(ord("A") + r), col_label=str(c + 1),
                    available=True, is_aisle=False,
                    seat_id=f"{chr(ord('A') + r)}{c + 1}",
                )
            )
    sm = SeatMap(n_rows=3, n_cols=16, seats=seats)
    block = find_best_block(sm, 4)
    assert block is not None
    cols = [s.col for s in block]
    assert cols[-1] - cols[0] == 3  # contiguous
    # must not span the 6-9 gap
    assert not (min(cols) < 6 and max(cols) >= 10)


def test_touches_aisle_helper():
    sm = make_map(1, 5, aisles={(0, 0), (0, 4)})
    selectable = [s for s in sm.row_seats(0) if s.available]  # cols 1,2,3
    # block cols 1,2 -> left edge touches the aisle at col 0
    assert _touches_aisle(selectable[:2], sm) is True
    # middle single seat col 2 -> neither neighbour is an aisle -> False
    assert _touches_aisle(selectable[1:2], sm) is False


def test_block_score_adds_aisle_bonus():
    args = dict(row=6, center_col=4.5, n_rows=10, n_cols=10, ideal_row_ratio=0.65)
    base = _block_score(touches_aisle=False, prefer_aisle=False, **args)
    with_bonus = _block_score(touches_aisle=True, prefer_aisle=True, **args)
    assert math.isclose(with_bonus - base, 0.05, abs_tol=1e-9)
    # bonus only applies when both the flag is set AND the block touches an aisle
    no_flag = _block_score(touches_aisle=True, prefer_aisle=False, **args)
    assert math.isclose(no_flag, base, abs_tol=1e-9)


def test_pick_alias_matches_find_best_block():
    sm = make_map(8, 8)
    assert pick(sm, 3) == find_best_block(sm, 3)
