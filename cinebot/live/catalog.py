"""Live Cineplex catalog used by the local top-to-bottom picker.

A short real-browser guest login obtains the same JWT/device key as the public
site. Read-only catalog calls then use the official Cineplex API. No booking is
created by this module.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..browse import CineplexClient
from ..group import display_show_time
from .auth import ORIGIN, _UA


class CatalogError(RuntimeError):
    """The live Cineplex catalog could not be loaded or normalized."""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def seat_catalog_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert get-seat data into browser-friendly grids grouped by seat type."""
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    result_types: list[dict[str, Any]] = []

    for raw_type in data.get("seatTypes") or []:
        type_id = _int(raw_type.get("seatTypeId"))
        title = str(raw_type.get("seatTypeTitle") or f"Class {type_id}")
        raw_seats = list(raw_type.get("seatStatus") or [])
        if not raw_seats:
            continue

        max_col = max(
            [_int(raw_type.get("seatColsCount"))]
            + [_int(item.get("colPosition")) for item in raw_seats]
        )
        row_positions = sorted(
            {_int(item.get("rowPosition")) for item in raw_seats}, reverse=True
        )
        by_row: dict[int, dict[int, dict[str, Any]]] = {}
        labels: dict[int, str] = {}

        for item in raw_seats:
            row_position = _int(item.get("rowPosition"))
            col_position = _int(item.get("colPosition"))
            label = str(item.get("seatTitle") or f"{row_position}-{col_position}")
            row_label = "".join(ch for ch in label if ch.isalpha()) or str(row_position)
            labels[row_position] = row_label
            by_row.setdefault(row_position, {})[col_position] = {
                "id": str(item.get("seatSeqId") or label),
                "label": label,
                "row": row_label,
                "col": label[len(row_label) :] or str(col_position),
                "cidx": col_position - 1,
                "status": "available" if _int(item.get("seatStatus")) == 1 else "taken",
                "seat_type_id": type_id,
                "seat_type_name": title,
            }

        rows: list[dict[str, Any]] = []
        for row_position in row_positions:
            cells: list[dict[str, Any]] = []
            row_cells = by_row.get(row_position, {})
            for col_position in range(1, max_col + 1):
                cell = row_cells.get(col_position)
                if cell is None:
                    cells.append(
                        {
                            "id": None,
                            "label": None,
                            "row": labels.get(row_position, ""),
                            "col": None,
                            "cidx": col_position - 1,
                            "status": "gap",
                            "seat_type_id": type_id,
                            "seat_type_name": title,
                        }
                    )
                else:
                    cells.append(cell)
            rows.append({"label": labels.get(row_position, ""), "cells": cells})

        result_types.append(
            {
                "id": type_id,
                "title": title,
                "capacity": _int(raw_type.get("seatCapacity"), len(raw_seats)),
                "n_cols": max_col,
                "rows": rows,
            }
        )

    return {
        "program_id": _int(data.get("programId")),
        "location_id": _int(data.get("locId")),
        "screen_id": _int(data.get("screenId")),
        "movie_id": _int(data.get("movieId")),
        "show_date": str(data.get("showDate") or ""),
        "show_time": str(data.get("showTime") or ""),
        "total_seats": _int(data.get("totalSeats")),
        "seat_types": result_types,
    }


def available_seats_by_label(
    raw: dict[str, Any], seat_type_id: int
) -> dict[str, dict[str, Any]]:
    """Return all available seats for one class, keyed by visible label."""
    catalog = seat_catalog_from_raw(raw)
    chosen = next(
        (item for item in catalog["seat_types"] if item["id"] == seat_type_id), None
    )
    if chosen is None:
        raise CatalogError("The selected seat class is no longer offered for this show.")
    seats: dict[str, dict[str, Any]] = {}
    for row in chosen["rows"]:
        for cell in row["cells"]:
            if cell["status"] == "available" and cell["label"]:
                seats[str(cell["label"]).upper()] = cell
    return seats


class CatalogManager:
    """Caches a short-lived guest API client for read-only picker requests."""

    def __init__(self) -> None:
        self._client: CineplexClient | None = None
        self._auth_lock = asyncio.Lock()

    async def _new_client(self) -> CineplexClient:
        from playwright.async_api import async_playwright

        last_error: Exception | None = None
        # Headless keeps catalog loading unobtrusive. If reCAPTCHA declines that
        # session, retry once with a normal Chrome window so the public flow can
        # complete naturally.
        for headless in (True, False):
            try:
                async with async_playwright() as pw:
                    try:
                        browser = await pw.chromium.launch(
                            channel="chrome", headless=headless
                        )
                    except Exception:
                        browser = await pw.chromium.launch(headless=headless)
                    context = await browser.new_context(
                        user_agent=_UA, viewport={"width": 1280, "height": 850}
                    )
                    page = await context.new_page()
                    try:
                        await page.goto(
                            ORIGIN, wait_until="domcontentloaded", timeout=30_000
                        )
                        button = page.locator("button.guest-login").first
                        await button.wait_for(state="visible", timeout=15_000)
                        async with page.expect_response(
                            lambda response: "/api/v1/guest-login" in response.url,
                            timeout=30_000,
                        ) as response_info:
                            await button.click()
                        response = await response_info.value
                        payload = await response.json()
                        token = str((payload.get("data") or {}).get("token") or "")
                        device_key = str(
                            response.request.headers.get("device-key") or ""
                        )
                        if not token or not device_key:
                            raise CatalogError(
                                "Cineplex guest login did not return a usable session."
                            )
                        return CineplexClient(device_key=device_key, token=token)
                    finally:
                        await context.close()
                        await browser.close()
            except Exception as exc:
                last_error = exc
        raise CatalogError(
            "Could not connect to the live Cineplex catalog. Try Load schedule again."
        ) from last_error

    async def _ensure_client(self) -> CineplexClient:
        if self._client is not None:
            return self._client
        async with self._auth_lock:
            if self._client is None:
                self._client = await self._new_client()
        return self._client

    async def _call(self, method: str, *args: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            client = await self._ensure_client()
            try:
                func: Callable[..., Any] = getattr(client, method)
                return await asyncio.to_thread(func, *args)
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    self._client = None
                    continue
        raise CatalogError(str(last_error) or "The Cineplex catalog request failed.")

    async def locations(self) -> list[dict[str, Any]]:
        raw = await self._call("get_locations")
        return [
            {
                "id": _int(item.get("id")),
                "title": str(item.get("locationTitle") or item.get("title") or ""),
                "address": str(item.get("address") or ""),
                "screens": _int(item.get("totalScreen")),
            }
            for item in raw
            if _int(item.get("id"))
        ]

    async def dates(self, location_id: int) -> list[dict[str, Any]]:
        raw = await self._call("get_showdates", location_id)
        dates: list[dict[str, Any]] = []
        for item in raw:
            movies = []
            for movie in item.get("availableMovies") or []:
                movie_id = _int(movie.get("movie_id") or movie.get("movieId"))
                if not movie_id:
                    continue
                movies.append(
                    {
                        "id": movie_id,
                        "title": str(
                            movie.get("movie_title")
                            or movie.get("movieTitle")
                            or movie.get("title")
                            or ""
                        ),
                        "language": str(movie.get("language") or ""),
                        "category": str(movie.get("category") or ""),
                        "length": str(movie.get("movie_length") or ""),
                        "image": str(
                            movie.get("profile_image")
                            or movie.get("cover_image")
                            or ""
                        ),
                    }
                )
            dates.append(
                {
                    "date": str(item.get("showDate") or ""),
                    "movies": movies,
                }
            )
        return dates

    async def shows(
        self, location_id: int, movie_id: int, show_date: str
    ) -> list[dict[str, Any]]:
        raw = await self._call("get_shows", location_id, movie_id, show_date)
        shows: list[dict[str, Any]] = []
        for screen in raw:
            screen_id = _int(screen.get("screenID") or screen.get("screenId"))
            hall_name = str(
                screen.get("screenTitle")
                or screen.get("screenName")
                or f"Hall {screen_id}"
            )
            movie_title = str(screen.get("movieTitle") or "")
            for slot in screen.get("showTimes") or []:
                program_id = _int(slot.get("programId"))
                show_time = str(slot.get("showTime") or "")
                if not program_id or not show_time:
                    continue
                prices = []
                for price in slot.get("seatPrices") or []:
                    prices.append(
                        {
                            "id": _int(
                                price.get("seatTypeId")
                                or price.get("seatTypeID")
                                or price.get("classId")
                            ),
                            "title": str(
                                price.get("seatTypeName")
                                or price.get("seatTypeTitle")
                                or price.get("seatType")
                                or "Seats"
                            ),
                            "price": _int(price.get("unitPrice")),
                        }
                    )
                shows.append(
                    {
                        "program_id": program_id,
                        "screen_id": screen_id,
                        "hall": hall_name,
                        "movie_id": _int(screen.get("movieId") or movie_id),
                        "movie_title": movie_title,
                        "date": str(screen.get("showDate") or show_date),
                        "time": show_time,
                        "time_label": display_show_time(show_time),
                        "seat_types": prices,
                    }
                )
        shows.sort(key=lambda item: (item["time"], item["hall"]))
        return shows

    async def seats(self, location_id: int, program_id: int) -> dict[str, Any]:
        raw = await self._call("get_seat_layout", location_id, program_id)
        return seat_catalog_from_raw(raw)

    async def raw_seats(self, location_id: int, program_id: int) -> dict[str, Any]:
        return await self._call("get_seat_layout", location_id, program_id)
