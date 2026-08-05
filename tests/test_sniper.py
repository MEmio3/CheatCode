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


def test_empty_rows_prioritizes_contiguous_middle_row_block():
    catalog = {
        "seat_types": [{
            "id": 1,
            "n_cols": 10,
            "rows": [
                {
                    "label": "A",
                    "cells": [
                        {"label": "A1", "status": "available", "cidx": 0},
                        {"label": "A2", "status": "available", "cidx": 1},
                        {"label": "A3", "status": "available", "cidx": 2},
                    ],
                },
                {
                    "label": "D",
                    "cells": [
                        {"label": "D1", "status": "available", "cidx": 0},
                        {"label": "D2", "status": "available", "cidx": 1},
                        {"label": "D3", "status": "available", "cidx": 2},
                        {"label": "D4", "status": "available", "cidx": 3},
                    ],
                },
                {
                    "label": "G",
                    "cells": [
                        {"label": "G1", "status": "available", "cidx": 0},
                        {"label": "G2", "status": "available", "cidx": 1},
                    ],
                },
            ],
        }]
    }
    # Requesting 3 seats in automatic mode should pick the centered contiguous block in middle row D (D2, D3, D4)
    assert compute_seat_plan(catalog, 1, [], 3) == ["D2", "D3", "D4"]


def test_case_insensitive_primary_rows_and_automatic_overflow():
    catalog = {
        "seat_types": [{
            "id": 1,
            "rows": [
                {"label": "e", "cells": [{"label": "E1", "status": "available", "cidx": 0}, {"label": "E2", "status": "available", "cidx": 1}, {"label": "E3", "status": "available", "cidx": 2}]},
                {"label": "f", "cells": [{"label": "F1", "status": "available", "cidx": 0}, {"label": "F2", "status": "available", "cidx": 1}]},
            ],
        }]
    }
    # primary row "E" trimming last 1 seat gets E1, E2; remaining seat auto-overflows to F1
    assert compute_seat_plan(catalog, 1, ["E"], 3, trim_last=1) == ["E1", "E2", "F1"]


def test_fuzzy_movie_title_matching():
    from cinebot.group import movie_matches
    assert movie_matches("Spider-Man: Brand New Day", "Spiderman Brand New Day")
    assert movie_matches("Avenger: Secret Wars", "Avengers Secret Wars")
    assert not movie_matches("Avatar: Fire and Ash", "Spider-Man: Brand New Day")


def test_validate_rejects_num_payments_greater_than_total_seats():
    cfg = config(total_seats=2, num_payments=3, attendees=[SnipeAttendee("A", "01711111111"), SnipeAttendee("B", "01722222222"), SnipeAttendee("C", "01733333333")])
    with pytest.raises(GroupPlanError):
        cfg.validate()


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

