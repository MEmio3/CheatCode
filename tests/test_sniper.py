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
