"""Concurrent multi-session real run: split N seats across k guest sessions and
fire them in parallel so the bKash OTPs arrive together.

Each session is an isolated browser context (its own guest login, its own cart),
runs the full chain to payment.bkash.com, enters the wallet number and clicks
Confirm -> bKash sends the OTP. We STOP at the OTP field; no OTP/PIN is entered.

Reads the bKash number from $CINEBOT_BKASH. Names are assigned per session.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from .auth import _UA, ORIGIN, guest_token_via_browser
from ..browse import CineplexClient
from ..seats.scorer import SeatMap

LOC_ID = 1
PROGRAM_ID = 129692  # Evil Dead Burn, Hall 6, 20:00
NAMES = ["Maruf", "Hossain", "Omio"]
HELD = {"J8", "J9", "J10", "J11", "H10", "H11"}  # held by earlier test runs


# ---- seat selection: a 4-row x 6-col block, split into chunks of <=10 --------====


def pick_block(seat_map: SeatMap, rows_needed: int, cols_needed: int, held: set[str]):
    """Best rectangular block (all available, not held, no aisle gaps)."""
    rows = seat_map.rows()
    best = None
    best_d = 1e18
    ir = 0.65 * (seat_map.n_rows - 1)
    ic = (seat_map.n_cols - 1) / 2.0
    for ri in range(len(rows) - rows_needed + 1):
        rs = rows[ri : ri + rows_needed]
        for c in range(seat_map.n_cols - cols_needed + 1):
            cells = []
            ok = True
            for r in rs:
                for cc in range(c, c + cols_needed):
                    s = seat_map.at(r, cc)
                    lab = f"{s.row_label}{s.col_label}" if s else None
                    if s is None or not s.available or lab in held:
                        ok = False
                        break
                    cells.append(s)
                if not ok:
                    break
            if ok and len(cells) == rows_needed * cols_needed:
                cr = sum(s.row for s in cells) / len(cells)
                ccn = sum(s.col for s in cells) / len(cells)
                d = ((cr - ir) ** 2 + (ccn - ic) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best = cells
    return best


def chunk_block(cells, cap: int = 10):
    """Row-major order, split into <=cap chunks."""
    ordered = sorted(cells, key=lambda s: (s.row, s.col))
    return [ordered[i : i + cap] for i in range(0, len(ordered), cap)]


# ---- async per-session flow ------------------------------------------------====


async def _dismiss_swal2(page):
    try:
        btn = page.locator(".swal2-confirm").first
        if await btn.is_visible(timeout=300):
            await btn.click(timeout=1000)
    except Exception:
        pass


async def _click_when_enabled(page, selector, timeout_ms=30000, label=""):
    import time as _t

    loc = page.locator(selector).first
    deadline = _t.time() + timeout_ms / 1000.0
    while _t.time() < deadline:
        await _dismiss_swal2(page)
        try:
            disabled = await loc.get_attribute("disabled")
            if disabled is None and await loc.is_visible():
                await loc.click(timeout=4000)
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return False


async def run_session(page, idx, name, seat_labels, bkash):
    log = lambda *a: print(f"[s{idx} {name}]".ljust(18), *a, flush=True)
    try:
        await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1200 + idx * 400)  # slight stagger
        await page.click("button.guest-login")
        await page.wait_for_timeout(2500)
        log("guest login")

        await page.locator("a").filter(has_text="Bashundhara Shopping Mall").first.click(timeout=8000)
        await page.wait_for_timeout(1500)
        await page.locator("text=29 Jul").first.click(timeout=5000)
        await page.wait_for_timeout(1500)
        await page.locator("text=Evil Dead Burn").first.click(timeout=5000)
        await page.wait_for_timeout(2000)
        await page.locator("div.card-wrap.ticket-time").filter(has_text="Hall 6").locator("a").first.click(timeout=6000)
        await page.wait_for_timeout(2500)
        log("showtime Hall 6 8:00 PM")

        await page.locator(".ticket_booking_left_item.seat_type").first.click(timeout=5000)
        await page.wait_for_timeout(600)
        await page.locator(".seat_type_select").get_by_text("Premium", exact=False).first.click(timeout=5000)
        await page.wait_for_timeout(1500)
        plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
        for _ in range(len(seat_labels)):
            await plus.click(timeout=4000)
            await page.wait_for_timeout(250)
        await page.wait_for_timeout(1000)
        log(f"qty {len(seat_labels)}")

        await _click_when_enabled(page, "button.btn-desktop-purchase", 30000, "reveal")
        await page.wait_for_timeout(2500)
        for lab in seat_labels:
            await _dismiss_swal2(page)
            try:
                await page.get_by_text(lab, exact=True).first.click(timeout=5000)
                await page.wait_for_timeout(300)
            except Exception as e:
                log(f"seat {lab} issue: {e}")
        await page.wait_for_timeout(1500)
        try:
            log("selected:", (await page.locator(".selected_seat").inner_text(timeout=2000)).replace("\n", " "))
        except Exception:
            pass

        try:
            await page.fill("input[name=customer_name]", name)
            await page.fill("input[name=msisdn]", bkash)
            await page.fill("input[name=msisdn_confirm]", bkash)
            try:
                await page.check("input[name=terms]")
            except Exception:
                await page.locator("input[name=terms]").first.click(force=True)
        except Exception as e:
            log("form issue:", e)

        await _click_when_enabled(page, "button.btn-desktop-purchase", 30000, "buy")
        await page.wait_for_timeout(6000)
        log("post-purchase:", page.url)

        # gateway
        try:
            await page.get_by_text("MOBILE BANKING", exact=False).first.wait_for(timeout=20000)
        except Exception:
            pass
        for sel in ["text=MOBILE BANKING", "text=Mobile Banking"]:
            try:
                await page.locator(sel).first.click(timeout=4000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(4500)
        for sel in ["img[src*=bkash]", "text=bKash", "[alt*=bkash]", "[class*=bkash]", "div[style*=bkash]"]:
            try:
                await page.locator(sel).first.click(timeout=2500)
                log("bKash via", sel)
                break
            except Exception:
                continue
        await page.wait_for_timeout(4500)

        # wallet + Confirm
        try:
            wallet = page.locator(
                'input[name="WALLET"], input[name*="WALLET"], input[placeholder*="01X"]'
            ).first
            await wallet.wait_for(timeout=15000)
            await wallet.fill(bkash)
            log(f"wallet ...{bkash[-4:]}")
            await page.wait_for_timeout(800)
            for sel in ["button:has-text('Confirm')", "[role=button]:has-text('Confirm')", "a:has-text('Confirm')"]:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.last.click(timeout=3000)
                    log("Confirm clicked")
                    break
            await page.wait_for_timeout(8000)
        except Exception as e:
            log("wallet/confirm issue:", e)

        try:
            info = await page.evaluate(
                "()=>({h:[...document.querySelectorAll('h1,h2,h3,h4')].map(e=>(e.textContent||'').trim().slice(0,40)).filter(Boolean),"
                "inp:[...document.querySelectorAll('input')].map(e=>({n:e.name,t:e.type})),u:location.href})"
            )
            log("OTP-PAGE?", info.get("h"), info.get("inp"))
        except Exception as e:
            log("dump err", e)
        await page.screenshot(path=f"E:/1-Ticket/multi_s{idx}_otp.png", full_page=True)
        return page.url
    except Exception as e:
        log("SESSION ERROR:", type(e).__name__, str(e)[:200])
        try:
            await page.screenshot(path=f"E:/1-Ticket/multi_s{idx}_err.png", full_page=True)
        except Exception:
            pass
        return None


async def amain(chunks, bkash):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctxs = [await browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 850}) for _ in chunks]
        pages = [await c.new_page() for c in ctxs]
        tasks = [
            run_session(pages[i], i, NAMES[i], [f"{s.row_label}{s.col_label}" for s in chunks[i]], bkash)
            for i in range(len(chunks))
        ]
        urls = await asyncio.gather(*tasks)
        print("\n=== session result URLs ===")
        for i, u in enumerate(urls):
            print(f"  s{i} {NAMES[i]}: {u}")
        await asyncio.sleep(60)  # leave windows up so OTPs are visible
        await browser.close()


def main() -> int:
    bkash = os.environ.get("CINEBOT_BKASH", "").strip()
    if not bkash:
        print("set CINEBOT_BKASH", file=sys.stderr)
        return 2

    # 1) get a token + live seat map, pick the block, split into <=10 chunks
    s = guest_token_via_browser(verbose=False)
    if not s["token"] or not s["device_key"]:
        print("no guest token; abort", file=sys.stderr)
        return 1
    c = CineplexClient(device_key=s["device_key"], token=s["token"])
    sm = CineplexClient.raw_seats_to_seatmap(c.get_seat_layout(LOC_ID, PROGRAM_ID))
    print(f"live map: {len(sm.seats)} seats, {sum(1 for x in sm.seats if x.available)} available; excluding held {sorted(HELD)}")
    block = pick_block(sm, 4, 6, HELD)
    if not block:
        print("no full 4x6 block available (held seats may fragment it).", file=sys.stderr)
        return 1
    labels = [f"{x.row_label}{x.col_label}" for x in sorted(block, key=lambda z: (z.row, z.col))]
    print("4x6 block:", labels)
    chunks = chunk_block(block, cap=10)
    print("split into", len(chunks), "sessions:", [[f"{y.row_label}{y.col_label}" for y in ch] for ch in chunks])

    # 2) run sessions concurrently
    asyncio.run(amain(chunks, bkash))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
