"""Pick the target show: Hall 6 at Bashundhara, tomorrow, cheapest with >=2 seats.

Outputs the chosen programId, showTime, unitPrice, and the best block of 2 from
the live seat map. The next step (booking) consumes that programId.
"""
from __future__ import annotations

import json
import sys

from .auth import guest_token_via_browser
from ..browse import CineplexClient
from ..seats.scorer import find_best_block

LOC_ID = 1  # Bashundhara City
TARGET_SCREEN = 6  # Hall 6
TOMORROW = "2026-07-29"


def main() -> int:
    s = guest_token_via_browser(verbose=True)
    if not s["token"]:
        print("no token; abort", file=sys.stderr)
        return 1
    c = CineplexClient(device_key=s["device_key"], token=s["token"])

    sds = c.get_showdates(LOC_ID)
    sd = next((x for x in sds if x.get("showDate") == TOMORROW), None)
    if sd is None:
        print(f"no shows for {TOMORROW}", file=sys.stderr)
        return 1

    hall6 = []
    for m in sd.get("availableMovies") or []:
        for sh in c.get_shows(LOC_ID, m.get("movie_id"), TOMORROW):
            if sh.get("screenID") != TARGET_SCREEN:
                continue
            for st in sh.get("showTimes") or []:
                prices = [p.get("unitPrice") for p in (st.get("seatPrices") or []) if p.get("unitPrice")]
                hall6.append({
                    "title": sh.get("movieTitle"),
                    "programId": st.get("programId"),
                    "profileId": st.get("profileId"),
                    "showTime": st.get("showTime"),
                    "minPrice": min(prices) if prices else None,
                })

    print(f"\nHall 6 tomorrow ({len(hall6)} show(s)):")
    candidates = []
    for h in hall6:
        try:
            raw = c.get_seat_layout(LOC_ID, h["programId"])
            sm = CineplexClient.raw_seats_to_seatmap(raw)
        except Exception as e:
            print(f"  [skip] program {h['programId']} seat fetch failed: {e}")
            continue
        block = find_best_block(sm, 2)
        avail = sum(1 for x in sm.seats if x.available)
        cand = {**h, "n_seats": len(sm.seats), "available": avail,
                "best2": [f"{b.row_label}{b.col_label}" for b in block] if block else None}
        candidates.append(cand)
        print(f"  {h['showTime']}  {h['title'][:34]:34}  price={h['minPrice']}  "
              f"seats={len(sm.seats)} avail={avail} best2={cand['best2']}")

    viable = [x for x in candidates if x["best2"]]
    viable.sort(key=lambda x: (x["minPrice"] if x["minPrice"] is not None else 1e9))
    chosen = viable[0] if viable else None
    print("\nCHOSEN:")
    print(json.dumps(chosen, ensure_ascii=False, indent=2) if chosen else "(no Hall 6 show has 2 contiguous seats)")
    return 0 if chosen else 1


if __name__ == "__main__":
    raise SystemExit(main())
