"""HTTP client for the Cineplex ticket API (discovered via passive recon).

Base + endpoint names + request bodies + gating are confirmed from the site's
own JS bundle (see docs/api-map.md). Two things this module depends on that must
be obtained from a REAL browser session, not raw HTTP:

  1. `device-key`  — a UUID the SPA generates and persists; the server may tie
     it to a session, so generate one and reuse it.
  2. Bearer token  — issued by `guest-login`, which is reCAPTCHA-v3 gated. Get
     it by driving a real browser (Playwright) to click "Guest Login"; the
     reCAPTCHA script scores the session naturally. We do NOT bypass reCAPTCHA.

Run from a Bangladesh network (the API 500s from non-BD IPs in testing).

Uses curl-cffi so the TLS/JA3 fingerprint looks like a real browser. Falls back
to plain requests with a warning if curl-cffi isn't installed.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

from .seats.scorer import Seat, SeatMap, seat_view

log = logging.getLogger("cinebot.browse")

BASE = "https://cineplex-ticket-api.cineplexbd.com/api/v1"
ORIGIN = "https://ticket.cineplexbd.com"

# Endpoint names (confirmed from the JS bundle).
EP_GUEST_LOGIN = "guest-login"
EP_LOGIN = "login"
EP_GET_LOCATION = "get-location"
EP_GET_SHOWDATE = "get-showdate"
EP_GET_SHOWS = "get-shows"
EP_GET_SEAT = "get-seat"
EP_BOOKING = "booking"
EP_BOOKING_CHECK = "bookingCheck"


def _new_session():
    try:
        from curl_cffi import requests as cc  # type: ignore

        return cc.Session(impersonate="chrome124")
    except ImportError:
        import requests  # type: ignore

        log.warning("curl_cffi not installed; using requests (may be JA3-fingerprinted).")
        return requests.Session()


class CineplexClient:
    def __init__(self, device_key: Optional[str] = None, token: Optional[str] = None):
        self.device_key = device_key or str(uuid.uuid4())
        self.token = token
        self.s = _new_session()

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "appsource": "web",
            "device-key": self.device_key,
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _post(self, endpoint: str, body: Optional[dict] = None) -> dict:
        url = f"{BASE}/{endpoint}"
        r = self.s.post(url, headers=self._headers(), json=body or {}, timeout=20)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"{endpoint} returned non-JSON (status {r.status_code}); "
                                f"likely geo/IP block or auth required")
        if data.get("status") == "error":
            raise RuntimeError(f"{endpoint} error: {data.get('message')}")
        return data

    # ---- auth -----------------------------------------------------------====

    def guest_login(self, recaptcha_token: str) -> str:
        """Exchange a reCAPTCHA v3 token for a guest JWT.

        Field name confirmed from a real capture: `recaptcha_token`. The token
        MUST be produced by the reCAPTCHA script in a real browser (site key
        6LchFI8qAAAAAO1tzM3d1sI2TFOzmRmd55G0BoX8). Capture it via Playwright.
        """
        data = self._post(EP_GUEST_LOGIN, {"recaptcha_token": recaptcha_token})
        token = (data.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"guest-login did not return a token: {data}")
        self.token = token
        return token

    @classmethod
    def from_capture(cls, jsonl_path: str) -> "CineplexClient":
        """Build a client reusing the device-key + Bearer token captured during
        a recon run. Handy for immediate live fetches without re-doing guest
        login (note: the token expires; re-capture when it does)."""
        import json

        device_key = None
        token = None
        with open(jsonl_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") != "request":
                    continue
                if r.get("device_key"):
                    device_key = r["device_key"]
                auth = r.get("authorization") or ""
                if auth.lower().startswith("bearer "):
                    token = auth.split(None, 1)[1]
        if not device_key:
            raise RuntimeError("no device-key found in capture")
        return cls(device_key=device_key, token=token)

    # ---- browse chain ---------------------------------------------------====

    def get_locations(self) -> list[dict]:
        """Theatres/branches. Body is empty (headers carry the gate)."""
        return (self._post(EP_GET_LOCATION, {}).get("data") or [])

    def get_showdates(self, location: Any) -> list[dict]:
        return (self._post(EP_GET_SHOWDATE, {"location": location}).get("data") or [])

    def get_shows(self, location: Any, movie_id: Any, show_date: Any) -> list[dict]:
        # body shape confirmed from the bundle: {location, movieId, showDate}
        return (
            self._post(EP_GET_SHOWS, {"location": location, "movieId": movie_id, "showDate": show_date})
            .get("data") or []
        )

    def get_seat_layout(self, location: Any, program_id: Any) -> dict:
        # body shape confirmed from the bundle: {location, programId}
        return self._post(EP_GET_SEAT, {"location": location, "programId": program_id}).get("data") or {}

    # ---- conversion into our SeatMap ------------------------------------====

    @staticmethod
    def raw_seats_to_seatmap(raw: dict) -> SeatMap:
        """Convert a real get-seat response into our pure SeatMap.

        Confirmed shape (from capture):
          data.seatTypes[] -> each { seatTypeTitle, seatRowCount, seatColsCount,
            seatStatus[] -> each { seatSeqId, seatTitle, rowPosition, colPosition,
                                   seatStatus } }
        seatStatus === 1 means available; anything else is disabled/booked.

        Orientation: the API numbers rows from the BACK (rowPosition 1 is the
        last letter, e.g. "N") toward the FRONT (the highest rowPosition is "A",
        nearest the screen). Our scorer convention is row 0 = nearest screen, so
        we flip: the highest rowPosition becomes row 0. Columns are 1-based;
        subtracting one keeps any internal column gap (aisle) intact for the
        contiguity check.
        """
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        raw_cells = []
        label_by_rp = {}
        max_cp = 0
        for st in data.get("seatTypes", []) or []:
            for cell in st.get("seatStatus", []) or []:
                rp = int(cell.get("rowPosition", 0))
                cp = int(cell.get("colPosition", 0))
                max_cp = max(max_cp, cp)
                title = str(cell.get("seatTitle") or f"{rp}-{cp}")
                row_label = ""
                for ch in title:
                    if ch.isalpha():
                        row_label += ch
                    else:
                        break
                row_label = row_label or str(rp)
                label_by_rp[rp] = row_label
                raw_cells.append(
                    (rp, cp, title, int(cell.get("seatStatus", 0)),
                     str(cell.get("seatSeqId") or title))
                )
        # Flip rows: highest rowPosition (front, "A") -> row 0. Descending.
        rps_desc = sorted(label_by_rp, reverse=True)
        rp_to_row = {rp: i for i, rp in enumerate(rps_desc)}
        seats = [
            Seat(
                row=rp_to_row[rp],
                col=cp - 1,  # 0-based; preserves column gaps (aisles)
                row_label=label_by_rp[rp],
                col_label=title[len(label_by_rp[rp]):] or str(cp),
                available=(status == 1),
                is_aisle=False,
                seat_id=seqid,
            )
            for rp, cp, title, status, seqid in raw_cells
        ]
        return SeatMap(n_rows=len(rps_desc), n_cols=max_cp, seats=seats)

    @staticmethod
    def seat_view_from_raw(raw: dict) -> dict:
        """One-shot: raw get-seat body -> UI seat view (no client needed).

        Used to render a real captured showtime offline; also what /api/seatmap
        serves when a live capture has been ingested.
        """
        return seat_view(CineplexClient.raw_seats_to_seatmap(raw))


def seat_view_from_capture(capture_path: str) -> dict:
    """Extract the get-seat response body from a recon JSONL and render it as a
    UI seat view.

    Scans the capture for the LAST get-seat response (a user often lands on the
    seat map more than once during recon; the final one reflects the showtime
    they actually explored). Returns the seat_view dict plus source metadata so
    the UI can label it as live.
    """
    import json

    chosen_body: Optional[dict] = None
    chosen_url: Optional[str] = None
    with open(capture_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "response":
                continue
            url = r.get("url") or ""
            if "get-seat" not in url:
                continue
            body = r.get("body") or ""
            if not body:
                continue
            try:
                chosen_body = json.loads(body)
                chosen_url = url
            except Exception:
                continue
    if chosen_body is None:
        raise RuntimeError(f"no get-seat response body found in {capture_path}")
    view = CineplexClient.seat_view_from_raw(chosen_body)
    view["source"] = "live"
    view["capture"] = os.path.basename(capture_path)
    view["endpoint"] = chosen_url
    meta = (chosen_body.get("data") or {}) if isinstance(chosen_body, dict) else {}
    for k in ("showDate", "showTime", "movieId", "locId", "screenId", "totalSeats"):
        if k in meta:
            view[k] = meta[k]
    return view


def main() -> int:
    """CLI: python -m cinebot.browse <capture.jsonl> <out.json>

    Extracts the real seat map from a recon capture and writes a seat-view JSON
    the UI can serve. Also prints a one-line summary so you can sanity-check the
    layout (rows, seats, best block) before wiring it in.
    """
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Extract a real seat map from a recon capture")
    ap.add_argument("capture", help="recon capture JSONL path")
    ap.add_argument("out", nargs="?", default="live_seats.json", help="output JSON path")
    args = ap.parse_args()

    view = seat_view_from_capture(args.capture)
    real = [s for r in view["rows"] for s in r["cells"] if s["status"] != "gap"]
    n_seats = len(real)
    n_avail = sum(1 for s in real if s["status"] == "available")
    labels = [r["label"] for r in view["rows"]]
    print(
        f"extracted seat map: rows={len(labels)} cols={view['n_cols']} "
        f"seats={n_seats} available={n_avail}\n"
        f"row labels: {labels}"
    )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(view, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
