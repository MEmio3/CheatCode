"""Browse the real Cineplex catalogue using a freshly captured guest token.

Runs: guest-login (headed browser) -> reuse token+device-key over HTTP ->
locations -> showdates -> shows. First run is exploratory: it prints the raw
shapes so we can confirm field names before adding selection logic.
"""
from __future__ import annotations

import json
import sys

from .auth import guest_token_via_browser
from ..browse import CineplexClient


def _snip(o, n=600) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)[:n]


def _looks_bashundhara(blob: dict) -> bool:
    return "bashundhara" in json.dumps(blob, ensure_ascii=False).lower()


def main() -> int:
    s = guest_token_via_browser(verbose=True)
    if not s["token"] or not s["device_key"]:
        print("no token/device-key; abort", file=sys.stderr)
        return 1
    print(f"[browse] token + device-key captured (token ...{s['token'][-8:]})", flush=True)

    c = CineplexClient(device_key=s["device_key"], token=s["token"])

    locs = c.get_locations()
    print(f"\nLOCATIONS ({len(locs)}):")
    for l in locs:
        keys = list(l.keys()) if isinstance(l, dict) else type(l).__name__
        print(f"  keys={keys}  snip={_snip(l, 220)}")

    bash = next((l for l in locs if isinstance(l, dict) and _looks_bashundhara(l)), None)
    if bash is None:
        print("\nno Bashundhara location found", file=sys.stderr)
        return 1
    print(f"\nBASHUNDHARA location full:\n  {json.dumps(bash, ensure_ascii=False, default=str)}")

    # the `location` param is probably the location id; try a few candidate keys
    cand = next((bash.get(k) for k in ("id", "locId", "locationId", "location_id") if bash.get(k) is not None), bash)
    print(f"\nusing location param = {cand!r}")
    try:
        sds = c.get_showdates(cand)
    except Exception as e:
        print(f"get_showdates failed: {e}", file=sys.stderr)
        return 1
    print(f"\nSHOWDATES ({len(sds)}); dates: " + ", ".join(str(s.get("showDate")) for s in sds if isinstance(s, dict)))
    if sds and isinstance(sds[0], dict):
        print(f"  showdate keys = {list(sds[0].keys())}")

    # find tomorrow (2026-07-29)
    TOMORROW = "2026-07-29"
    sd = next((s for s in sds if isinstance(s, dict) and s.get("showDate") == TOMORROW), None)
    if sd is None:
        print(f"\nno showdate for {TOMORROW}; available: {[s.get('showDate') for s in sds]}", file=sys.stderr)
        return 1
    movies = sd.get("availableMovies") or []
    print(f"\n{TOMORROW}: {len(movies)} movies. Probing shows for each...")
    for m in movies:
        mid = m.get("movie_id") or m.get("movieId") or m.get("id")
        title = m.get("movie_title") or m.get("title") or "?"
        try:
            shows = c.get_shows(cand, mid, TOMORROW)
        except Exception as e:
            print(f"  [skip] {title!r} get_shows: {e}")
            continue
        if not shows:
            continue
        # learn the shape once
        keys = list(shows[0].keys()) if isinstance(shows[0], dict) else None
        print(f"\n  MOVIE {mid} {title!r}: {len(shows)} show(s); keys={keys}")
        for sh in shows:
            print("    " + _snip(sh, 320))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
