import pytest

from cinebot.group import GroupPlanError
from cinebot.sniper import SnipeAttendee, SnipeConfig, SniperManager, compute_seat_plan


def config(**changes):
    values = {
        "target_movie": "Spider-Man: Brand New Day",
        "location_id": 1,
        "location_name": "Bashundhara",
        "hall_ids": [],
        "show_date": "2026-08-01",
        "time_start": "",
        "time_end": "",
        "total_seats": 2,
        "primary_rows": ["E"],
        "fill_row": "F",
        "num_payments": 1,
        "attendees": [SnipeAttendee("A One", "01712345678")],
    }
    values.update(changes)
    return SnipeConfig(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"show_date": "not-a-date"},
        {"time_start": "10:00"},
        {"time_start": "18:00", "time_end": "17:00"},
        {"hall_ids": [6, 6]},
        {"primary_rows": ["E", "E"]},
        {"primary_rows": ["E"], "fill_row": "E"},
    ],
)
def test_watcher_rejects_ambiguous_or_impossible_preferences(changes):
    with pytest.raises(GroupPlanError):
        config(**changes).validate()


def test_watcher_allows_empty_hall_and_time_preferences():
    cfg = config()
    cfg.validate()
    manager = SniperManager(None, None)
    show = manager._pick_show(
        [
            {"screen_id": 8, "time": "12:30"},
            {"screen_id": 6, "time": "18:30"},
        ],
        cfg,
    )
    assert show == {"screen_id": 8, "time": "12:30"}


def test_watcher_uses_hall_and_time_preferences_when_supplied():
    cfg = config(hall_ids=[6, 7], time_start="16:00", time_end="18:00")
    cfg.validate()
    manager = SniperManager(None, None)
    assert manager._pick_show(
        [
            {"screen_id": 5, "time": "17:00"},
            {"screen_id": 6, "time": "15:59"},
            {"screen_id": 7, "time": "17:30"},
        ],
        cfg,
    ) == {"screen_id": 7, "time": "17:30"}


def test_empty_rows_use_live_layout_automatically():
    catalog = {
        "seat_types": [{
            "id": 1,
            "rows": [
                {"label": "B", "cells": [{"label": "B1", "status": "available", "cidx": 0}]},
                {"label": "A", "cells": [{"label": "A1", "status": "available", "cidx": 0}]},
            ],
        }]
    }
    assert compute_seat_plan(catalog, 1, [], "", 2) == ["B1", "A1"]


@pytest.mark.asyncio
async def test_telegram_status_is_rate_limited_when_credentials_are_absent(monkeypatch):
    class Store:
        calls = 0

        def get(self, key):
            self.calls += 1
            return None

    store = Store()
    monkeypatch.setattr("cinebot.sniper.CredentialStore.auto", lambda: store)
    monkeypatch.setattr("cinebot.sniper.telegram_env_credentials", lambda: (None, None))
    manager = SniperManager(None, None)
    cfg = config()

    await manager._report(cfg, "Still waiting.")
    await manager._report(cfg, "Still waiting.")

    assert store.calls == 2  # token + chat ID on the first report only


# --- cohesive seat selection ------------------------------------------------=


def _row(label, n, taken=()):
    """A row of ``n`` seats (1-based); seats whose index is in ``taken`` are booked."""
    cells = []
    for i in range(1, n + 1):
        cells.append(
            {"label": f"{label}{i}", "status": "taken" if i in taken else "available", "cidx": i - 1}
        )
    return {"label": label, "cells": cells}


def _catalog(rows, n_cols, seat_type_id=1):
    return {"seat_types": [{"id": seat_type_id, "n_cols": n_cols, "rows": rows}]}


def _fragments(labels, catalog, seat_type_id=1):
    """Contiguous fragment sizes among the selected labels (sorted)."""
    seat_type = next(st for st in catalog["seat_types"] if st["id"] == seat_type_id)
    selected = set(labels)
    frags = []
    for row in seat_type["rows"]:
        cells = sorted(
            (
                c
                for c in row["cells"]
                if c.get("status") == "available" and c.get("label") in selected
            ),
            key=lambda c: int(c["cidx"]),
        )
        run, prev = 0, None
        for c in cells:
            cidx = int(c["cidx"])
            if prev is not None and cidx != prev + 1:
                frags.append(run)
                run = 0
            run += 1
            prev = cidx
        if run:
            frags.append(run)
    return sorted(frags)


def test_cohesive_keeps_full_empty_row_no_trim():
    catalog = _catalog([_row("E", 10)], n_cols=10)
    result = compute_seat_plan(catalog, 1, ["E"], "", 10)
    assert len(result) == 10
    assert set(result) == {f"E{i}" for i in range(1, 11)}


def test_cohesive_splits_balanced_when_no_row_fits():
    # Each row has only a 6-seat run; a group of 10 must split across E and F.
    catalog = _catalog([_row("E", 10, taken=(7, 8, 9, 10)), _row("F", 10, taken=(7, 8, 9, 10))], n_cols=10)
    result = compute_seat_plan(catalog, 1, ["E", "F"], "", 10, tolerance=3)
    assert len(result) == 10
    frags = _fragments(result, catalog)
    assert min(frags) >= 3  # nobody isolated
    assert len(frags) == 2  # one balanced block per row


def test_cohesive_group_larger_than_row_uses_balanced_split():
    # 11 people in rows of 10 -> split 6+5, never 10+1.
    catalog = _catalog([_row("E", 10), _row("F", 10)], n_cols=10)
    result = compute_seat_plan(catalog, 1, ["E", "F"], "", 11, tolerance=3)
    assert len(result) == 11
    per_row = [
        sum(1 for s in result if s.startswith("E")),
        sum(1 for s in result if s.startswith("F")),
    ]
    assert sorted(per_row) == [5, 6]
    assert min(_fragments(result, catalog)) >= 3


def test_force_grabs_any_seats_ignoring_gaps():
    catalog = _catalog([_row("E", 10, taken=(5, 6)), _row("F", 10)], n_cols=10)
    result = compute_seat_plan(catalog, 1, ["E"], "F", 6, force=True)
    # Force scans the preferred row first, across the taken gap, before moving on.
    assert result == ["E1", "E2", "E3", "E4", "E7", "E8"]


def test_preferred_row_unfit_expands_to_adjacent_row():
    # E only fits 3; a group of 8 expands to F instead of failing.
    catalog = _catalog([_row("E", 10, taken=(4, 5, 6, 7, 8, 9, 10)), _row("F", 10)], n_cols=10)
    result = compute_seat_plan(catalog, 1, ["E"], "", 8, tolerance=3)
    assert len(result) == 8
    assert all(s.startswith("F") for s in result)


# --- all-locations scan -----------------------------------------------------#


class _FakeCatalog:
    """Three branches; the movie is listed at 1 and 2 but only branch 2 has a
    matching show, so a scan must look past branch 1 to find it."""

    def __init__(self, matching_loc=2):
        self.matching_loc = matching_loc

    async def locations(self):
        return [
            {"id": 1, "title": "Bashundhara"},
            {"id": 2, "title": "Shimanto"},
            {"id": 3, "title": "Sony"},
        ]

    async def dates(self, loc_id):
        movies = (
            [{"id": 10, "title": "Spider-Man: Brand New Day"}]
            if loc_id in (1, 2) else []
        )
        return [{"date": "2026-08-01", "movies": movies}]

    async def shows(self, loc_id, movie_id, show_date):
        if loc_id != self.matching_loc:
            return []
        return [{
            "screen_id": 6, "time": "17:00", "program_id": 999,
            "hall": "Hall 6", "time_label": "5:00 PM",
            "movie_title": "Spider-Man: Brand New Day",
            "seat_types": [{"id": 1, "title": "Premium", "price": 600}],
        }]

    async def seats(self, loc_id, program_id):
        return _catalog([_row("E", 10), _row("F", 10)], n_cols=10)


class _FakeGroup:
    def __init__(self):
        self.started = None

    async def start(self, target, payments, allow_duplicate_identity=False):
        self.started = (target, payments)


def _no_telegram(monkeypatch):
    monkeypatch.setattr(
        "cinebot.sniper.CredentialStore.auto",
        lambda: type("S", (), {"get": lambda self, k: None})(),
    )
    monkeypatch.setattr("cinebot.sniper.telegram_env_credentials", lambda: (None, None))


def test_all_locations_makes_location_optional():
    config(all_locations=True, location_id=0, location_name="").validate()


def test_single_location_still_required_without_all_locations():
    cfg = config(all_locations=False, location_id=0, location_name="")
    with pytest.raises(GroupPlanError):
        cfg.validate()


@pytest.mark.asyncio
async def test_all_locations_scans_past_branches_without_a_show(monkeypatch):
    _no_telegram(monkeypatch)
    catalog = _FakeCatalog(matching_loc=2)
    group = _FakeGroup()
    manager = SniperManager(catalog, group)
    cfg = config(all_locations=True, location_id=0, location_name="")
    cfg.validate()

    await manager._poll_once(cfg)

    assert group.started is not None
    target, _payments = group.started
    assert target["location_id"] == 2
    assert target["location_name"] == "Shimanto"


@pytest.mark.asyncio
async def test_all_locations_without_any_match_does_not_fire(monkeypatch):
    _no_telegram(monkeypatch)
    catalog = _FakeCatalog(matching_loc=None)  # no branch exposes a show
    group = _FakeGroup()
    manager = SniperManager(catalog, group)
    cfg = config(all_locations=True, location_id=0, location_name="")
    cfg.validate()

    await manager._poll_once(cfg)

    assert group.started is None
    assert manager.status == "watching"
    assert "3 location" in manager.detail
