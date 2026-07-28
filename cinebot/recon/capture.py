"""Recon capture: record the live booking flow so we can map the API.

This script is OBSERVER-ONLY. It launches a real headed Chromium, points it at
ticket.cineplexbd.com, and logs every request/response to a JSONL file while
YOU manually log in, pick a movie, choose seats, and walk up to payment. It
deliberately auto-submits nothing — recon must look like a normal user.

The single most important measurement is the seat-hold TTL, so responses whose
bodies/headers mention hold / expire / timer / ttl / lock are flagged live and
in the end-of-run summary.

Run with:  python -m cinebot.recon.capture
Then fill in docs/api-map.md from the printed summary + the JSONL capture.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

# Terms that hint at the seat-hold lifecycle (the architectural driver) plus
# auth/session surfaces we need to document.
HOLD_KEYWORDS = ("hold", "expire", "expiry", "timer", "ttl", "lock", "reserve", "release")
AUTH_KEYWORDS = ("authorization", "token", "jwt", "csrf", "x-csrf", "session")

_SNIPPET_BYTES = 2048


def _normalize(url: str) -> str:
    """Collapse an endpoint to a pattern for grouping (strip query + ids)."""
    p = urlparse(url)
    path = p.path or "/"
    parts: list[str] = []
    for seg in path.split("/"):
        if seg == "":
            continue
        if re.fullmatch(r"\d+", seg) or re.fullmatch(r"[0-9a-fA-F]{8,}", seg):
            parts.append(":id")
        else:
            parts.append(seg)
    return f"{p.netloc}/{'/'.join(parts)}"


def _flags(text: str, words: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [w for w in words if w in low]


def main() -> int:
    ap = argparse.ArgumentParser(description="Recon-capture the Cineplex booking flow")
    ap.add_argument("--out", default=None, help="output JSONL path")
    ap.add_argument("--url", default="https://ticket.cineplexbd.com/")
    args = ap.parse_args()

    out_dir = "recon_captures"
    os.makedirs(out_dir, exist_ok=True)
    out_path = args.out or os.path.join(out_dir, f"capture-{int(time.time())}.jsonl")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    endpoints: Counter = Counter()
    flagged: list[dict] = []
    auth_seen: set[str] = set()
    started = time.time()

    with open(out_path, "w", encoding="utf-8") as out:

        def write(obj: dict) -> None:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out.flush()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()

            def on_request(req) -> None:  # noqa: ANN001
                try:
                    headers = {k.lower(): v for k, v in req.headers.items()}
                    auth_hits = _flags(json.dumps(headers), AUTH_KEYWORDS)
                    if auth_hits:
                        auth_seen.update(auth_hits)
                    is_api = "cineplex-ticket-api" in req.url
                    write({
                        "ts": round(time.time() - started, 3),
                        "kind": "request",
                        "method": req.method,
                        "url": req.url,
                        "endpoint": _normalize(req.url),
                        "auth_hits": auth_hits,
                        # for the real API, keep the body + the two gating headers
                        "post_data": (req.post_data if is_api else None),
                        "device_key": (headers.get("device-key") if is_api else None),
                        "authorization": (headers.get("authorization") if is_api else None),
                    })
                except Exception:
                    pass

            def on_response(resp) -> None:  # noqa: ANN001
                try:
                    endpoint = _normalize(resp.url)
                    endpoints[(resp.request.method, endpoint, resp.status)] += 1
                    is_api = "cineplex-ticket-api" in resp.url
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    ctype = headers.get("content-type", "")
                    text = ""
                    if "json" in ctype or "text" in ctype or "html" in ctype:
                        try:
                            # full body for API calls (what we need to wire live seats);
                            # snippet only for everything else
                            text = resp.text()[: (50000 if is_api else _SNIPPET_BYTES)]
                        except Exception:
                            text = ""
                    blob = text + " " + json.dumps(headers)
                    flags = _flags(blob, HOLD_KEYWORDS) if not is_api else []
                    auth_hits = _flags(json.dumps(headers), AUTH_KEYWORDS)
                    if auth_hits:
                        auth_seen.update(auth_hits)
                    rec = {
                        "ts": round(time.time() - started, 3),
                        "kind": "response",
                        "method": resp.request.method,
                        "url": resp.url,
                        "endpoint": endpoint,
                        "status": resp.status,
                        "content_type": ctype,
                        "body": text if is_api else (text if flags else ""),
                        "hold_flags": flags,
                        "auth_hits": auth_hits,
                    }
                    write(rec)
                    if flags:
                        flagged.append(rec)
                        print(f"  [HOLD?] {resp.request.method} {endpoint} -> {resp.status} flags={flags}")
                    elif is_api:
                        print(f"  [API] {resp.request.method} {endpoint} -> {resp.status} ({len(text)}b saved)")
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)

            print(f"Recon capture -> {out_path}")
            print(f"Opening {args.url}\n")
            page.goto(args.url)
            print(
                "INSTRUCTIONS (you drive, the script only watches):\n"
                "  1. Log in with your phone number; enter the OTP yourself.\n"
                "  2. Pick a movie, then a showtime.\n"
                "  3. On the seat map, note how long seats stay held (a timer\n"
                "     usually appears). Pick seats.\n"
                "  4. Proceed until the payment (SSL Commerz / bKash) page, then STOP\n"
                "     - do not pay.\n"
                "  5. Close the browser window, or press Ctrl+C here, to end capture.\n"
            )

            try:
                while not page.is_closed():
                    page.wait_for_timeout(500)
            except KeyboardInterrupt:
                pass

            try:
                context.close()
                browser.close()
            except Exception:
                pass

    # ---- summary ----------------------------------------------------------====
    print("\n" + "=" * 70)
    print("RECON SUMMARY")
    print("=" * 70)
    print(f"\nAuth surface keywords seen on requests: {sorted(auth_seen) or 'none'}")
    print("\nEndpoints hit (method  endpoint  status -> count):")
    for (method, endpoint, status), count in sorted(endpoints.items()):
        print(f"  {method:5} {status} {endpoint}  x{count}")

    if flagged:
        print(f"\nFLAGGED seat-hold / timer responses ({len(flagged)}):")
        for r in flagged:
            print(f"  {r['method']} {r['endpoint']} -> {r['status']}  flags={r['hold_flags']}")
    else:
        print("\nNo hold/timer keywords detected — check the seat-map responses manually.")
        print("The TTL may be returned only on the seat-hold POST, or enforced server-side.")

    print(f"\nFull capture: {out_path}")
    print("Next: fill in docs/api-map.md. Priority #1 = the seat-hold TTL value.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
