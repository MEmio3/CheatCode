"""Direct-API booking probe.

Harvest a guest JWT + device-key + a reCAPTCHA v3 token from one real browser,
then POST /api/v1/booking via HTTP (curl-cffi). Tests whether the booking step
can skip seat-clicking entirely. The payment leg (SSL Commerz -> bKash) still
needs a browser, so this module only covers booking for now.

Run from the UI "Test API booking" button, or:

    python -m cinebot.live.api_booking --movie-id 1705 --program-id 129861 ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from typing import Any

from ..browse import BASE, CineplexClient, ORIGIN
from ..group import display_show_time
from .auth import _UA

log = logging.getLogger("cinebot.live.api")

# reCAPTCHA v3 site key for ticket.cineplexbd.com (see docs/api-map.md).
SITE_KEY = "6LchFI8qAAAAAO1tzM3d1sI2TFOzmRmd55G0BoX8"


async def harvest_tokens(n: int, headless: bool = False) -> tuple[str, str, list[str]]:
    """Guest-login once, then mint N single-use reCAPTCHA tokens in the same
    browser session. Returns (device_key, jwt, [tokens])."""
    from playwright.async_api import async_playwright

    n = max(1, n)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=headless)
        except Exception:
            browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=_UA, viewport={"width": 1280, "height": 850}
        )
        page = await ctx.new_page()
        try:
            await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30_000)
            button = page.get_by_role("button", name="Guest Login", exact=True)
            await button.wait_for(state="visible", timeout=15_000)
            async with page.expect_response(
                lambda r: "/api/v1/guest-login" in r.url, timeout=30_000
            ) as ri:
                await button.click()
            resp = await ri.value
            payload = await resp.json()
            token = str((payload.get("data") or {}).get("token") or "")
            device_key = str(resp.request.headers.get("device-key") or "")
            if not token or not device_key:
                raise RuntimeError("guest login did not return token + device-key")
            tokens: list[str] = []
            for _ in range(n):
                tk = await page.evaluate(
                    """async (siteKey) => {
                        await new Promise(r => grecaptcha.ready(r));
                        return await grecaptcha.execute(siteKey, {action: 'booking'});
                    }""",
                    SITE_KEY,
                )
                if tk:
                    tokens.append(str(tk))
            if not tokens:
                raise RuntimeError("could not mint any reCAPTCHA tokens")
            return device_key, token, tokens
        finally:
            await ctx.close()
            await browser.close()


async def harvest_session(headless: bool = False) -> tuple[str, str, str]:
    """Open a browser, click Guest Login, then mint a reCAPTCHA token.

    Returns (device_key, jwt, recaptcha_token). The browser may be visible or
    headless; reCAPTCHA v3 scores a real session either way.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=headless)
        except Exception:
            browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=_UA, viewport={"width": 1280, "height": 850}
        )
        page = await ctx.new_page()
        try:
            await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30_000)
            button = page.get_by_role("button", name="Guest Login", exact=True)
            await button.wait_for(state="visible", timeout=15_000)
            async with page.expect_response(
                lambda r: "/api/v1/guest-login" in r.url, timeout=30_000
            ) as ri:
                await button.click()
            resp = await ri.value
            payload = await resp.json()
            token = str((payload.get("data") or {}).get("token") or "")
            device_key = str(resp.request.headers.get("device-key") or "")
            if not token or not device_key:
                raise RuntimeError("guest login did not return token + device-key")
            recaptcha_token = await page.evaluate(
                """async (siteKey) => {
                    if (typeof grecaptcha === 'undefined')
                        throw new Error('grecaptcha not loaded on the page');
                    await new Promise(r => grecaptcha.ready(r));
                    return await grecaptcha.execute(siteKey, {action: 'booking'});
                }""",
                SITE_KEY,
            )
            if not recaptcha_token:
                raise RuntimeError("grecaptcha.execute returned an empty token")
            return device_key, token, str(recaptcha_token)
        finally:
            await ctx.close()
            await browser.close()


def build_booking_body(
    target: dict[str, Any], payment: dict[str, Any], seat_seq_ids: list[str]
) -> dict[str, Any]:
    """Build the /booking request body from the shape captured from a real run.

    Field aliases (hall_id<-screen_id, schedule_id<-program_id) are best-effort
    and exactly what the probe is meant to validate against the live response.
    """
    labels = [str(l).upper() for l in (payment.get("seats") or [])]
    count = len(labels)
    unit_price = int(target.get("unit_price") or 0)
    phone = str(payment.get("bkash_number") or "")
    return {
        "req_id": random.randint(10_000_000, 99_999_999),
        "movie_id": int(target.get("movie_id") or 0),
        "hall_id": int(target.get("screen_id") or 0),
        "loc_id": int(target.get("location_id") or 0),
        "schedule_id": int(target.get("program_id") or 0),
        "total_ticket": count,
        "seatTypeId": int(target.get("seat_type_id") or 0),
        "total_amount": unit_price * count,
        "ticket_amount": unit_price * count,
        "addon_amount": 0,
        "msisdn": phone,
        "customer_name": str(payment.get("name") or ""),
        "seat_no": ", ".join(str(s) for s in seat_seq_ids),
        "seat_name": ", ".join(labels),
        "show_date": str(target.get("show_date") or ""),
        "show_time": display_show_time(str(target.get("show_time") or "")),
        "ticket_for": "others",
        "addons": "[]",
        "payment_source": "ssl",
        "msisdn_confirm": phone,
        # recaptcha_token is injected by post_booking from the harvested token.
    }


def post_booking(
    device_key: str, jwt: str, recaptcha_token: str, body: dict[str, Any]
) -> dict[str, Any]:
    """POST /booking via HTTP, returning {'http_status', 'body'} regardless of
    success/failure so the probe can show the raw server response."""
    client = CineplexClient(device_key=device_key, token=jwt)
    payload = {**body, "recaptcha_token": recaptcha_token}
    r = client.s.post(
        f"{BASE}/booking", headers=client._headers(), json=payload, timeout=25
    )
    try:
        data = r.json()
    except Exception:
        data = {"raw": (r.text or "")[:1000]}
    return {"http_status": r.status_code, "body": data}


def post_purchase(device_key: str, jwt: str, booking_id: str) -> dict[str, Any]:
    """POST /purchase via HTTP to capture the SSL Commerz handoff (the gateway
    URL or a redirect Location). Returns http_status, body, and any Location."""
    client = CineplexClient(device_key=device_key, token=jwt)
    r = client.s.post(
        f"{BASE}/purchase",
        headers=client._headers(),
        json={"booking_id": booking_id},
        timeout=25,
        allow_redirects=False,
    )
    try:
        data = r.json()
    except Exception:
        data = {"raw": (r.text or "")[:1000]}
    return {
        "http_status": r.status_code,
        "body": data,
        "location": r.headers.get("location"),
    }


async def probe(
    target: dict[str, Any],
    payment: dict[str, Any],
    seat_seq_ids: list[str],
    *,
    headless: bool = False,
) -> dict[str, Any]:
    """Harvest a session, then POST /booking. Returns a result envelope with
    the request body and the raw response so the caller can see the shape."""
    try:
        device_key, jwt, recaptcha_token = await harvest_session(headless=headless)
    except Exception as exc:
        log.warning("API booking harvest failed: %s", exc)
        return {"ok": False, "stage": "harvest", "error": str(exc)}
    try:
        body = build_booking_body(target, payment, seat_seq_ids)
        response = await asyncio.to_thread(
            post_booking, device_key, jwt, recaptcha_token, body
        )
        ok = response.get("http_status") == 200 and response.get("body", {}).get("code") == 200
        result: dict[str, Any] = {
            "ok": ok,
            "stage": "booking",
            "request_body": body,
            "response": response,
        }
    except Exception as exc:
        log.warning("API booking POST failed: %s", exc)
        return {"ok": False, "stage": "booking", "error": str(exc)}

    if ok:
        booking_id = str(
            ((response.get("body") or {}).get("data") or {}).get("booking_id") or ""
        )
        if booking_id:
            try:
                purchase = await asyncio.to_thread(
                    post_purchase, device_key, jwt, booking_id
                )
                result["purchase"] = purchase
            except Exception as exc:
                result["purchase_error"] = str(exc)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe direct-API /booking")
    ap.add_argument("--movie-id", type=int, required=True)
    ap.add_argument("--program-id", type=int, required=True)
    ap.add_argument("--location-id", type=int, required=True)
    ap.add_argument("--screen-id", type=int, required=True)
    ap.add_argument("--seat-type-id", type=int, required=True)
    ap.add_argument("--unit-price", type=int, default=0)
    ap.add_argument("--show-date", required=True)
    ap.add_argument("--show-time", required=True, help="HH:MM")
    ap.add_argument("--seat-ids", required=True, help="comma-separated seatSeqIds")
    ap.add_argument("--seat-labels", required=True, help="comma-separated labels")
    ap.add_argument("--name", required=True)
    ap.add_argument("--bkash", required=True)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    target = {
        "movie_id": args.movie_id,
        "program_id": args.program_id,
        "location_id": args.location_id,
        "screen_id": args.screen_id,
        "seat_type_id": args.seat_type_id,
        "unit_price": args.unit_price,
        "show_date": args.show_date,
        "show_time": args.show_time,
    }
    payment = {"name": args.name, "bkash_number": args.bkash, "seats": args.seat_labels.split(",")}
    seat_seq_ids = [s.strip() for s in args.seat_ids.split(",") if s.strip()]
    result = asyncio.run(probe(target, payment, seat_seq_ids, headless=args.headless))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
