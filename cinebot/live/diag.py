"""Diagnostic: drive ONE session through the real booking flow exactly the way
group_booking._run_payment_session does, but with a screenshot + DOM presence
check after every step, and STOP before the buy click (nothing is booked).

Run headed so we can see it:
    python -m cinebot.live.diag
Prints a per-step report so we can see which selector breaks.
"""
from __future__ import annotations

import json
import os

from .auth import _UA, ORIGIN

SHOW = {
    "location": "Bashundhara Shopping Mall",
    "date_text": "29 Jul",
    "movie": "Evil Dead Burn",
    "hall": "Hall 6",
    "time_label": "8:00 PM",
    "seat_type": "Premium",
    "seats": ["M1", "M2"],  # row M, away from prior test holds
    "name": "Diag Test",
    "phone": "01700000001",
}
SHOT_DIR = "E:/1-Ticket"


def _shot(page, name):
    try:
        page.screenshot(path=f"{SHOT_DIR}/{name}.png", full_page=False)
    except Exception:
        pass


def _dom(page):
    try:
        return page.evaluate(
            """() => ({
              url: location.href,
              purchase_btn: (() => { const b = document.querySelector('button.btn-desktop-purchase');
                return b ? {disabled: b.disabled, visible: b.offsetParent !== null} : null; })(),
              seat_type_open: !!document.querySelector('.ticket_booking_left_item.seat_type'),
              seat_type_select: !!document.querySelector('.seat_type_select'),
              qty_plus: !!document.querySelector('.ticket_qty_view div:nth-child(3) img'),
              seat_anchors: document.querySelectorAll('a.default, a.seat, [data-seat]').length,
              selected_seat: (document.querySelector('.selected_seat')||{}).innerText || '',
              name_input: !!document.querySelector('input[name=customer_name]'),
              msisdn_input: !!document.querySelector('input[name=msisdn]'),
              terms_input: !!document.querySelector('input[name=terms]'),
              body_head: (document.body.innerText||'').slice(0,160)
            })"""
        )
    except Exception as e:
        return {"dom_err": str(e)}


def main() -> int:
    import time as _t
    from playwright.sync_api import sync_playwright

    report = []

    def step(name, **extra):
        report.append({"step": name, **extra})
        print(f"[{name}] {json.dumps(extra, ensure_ascii=False)[:300]}", flush=True)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = pw.chromium.launch(headless=False)
        page = browser.new_context(user_agent=_UA, viewport={"width": 1320, "height": 900}).new_page()

        try:
            # 1) guest login
            page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            page.click("button.guest-login")
            page.wait_for_timeout(2500)
            step("guest_login", url=page.url)

            # 2) navigate
            page.locator("a").filter(has_text=SHOW["location"]).first.click(timeout=8000)
            page.wait_for_timeout(1500)
            page.get_by_text(SHOW["date_text"], exact=False).first.click(timeout=5000)
            page.wait_for_timeout(1500)
            page.get_by_text(SHOW["movie"], exact=False).first.click(timeout=5000)
            page.wait_for_timeout(2000)
            page.locator("div.card-wrap.ticket-time").filter(
                has_text=SHOW["hall"]
            ).locator("a").first.click(timeout=6000)
            page.wait_for_timeout(2500)
            step("navigate_show", **_dom(page))
            _shot(page, "diag_navigate")

            # 3) seat type
            page.locator(".ticket_booking_left_item.seat_type").first.click(timeout=5000)
            page.wait_for_timeout(600)
            page.locator(".seat_type_select").get_by_text(SHOW["seat_type"], exact=False).first.click(timeout=5000)
            page.wait_for_timeout(1500)
            step("seat_type", **_dom(page))

            # 4) qty
            plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
            plus.click(timeout=4000); page.wait_for_timeout(300)
            plus.click(timeout=4000); page.wait_for_timeout(1000)
            step("qty_2", **_dom(page))
            _shot(page, "diag_qty")

            # 5) reveal purchase
            purchase = page.locator("button.btn-desktop-purchase").first
            ok = True
            try:
                deadline = _t.time() + 20
                while _t.time() < deadline:
                    if purchase.is_visible() and not purchase.get_attribute("disabled"):
                        break
                    page.wait_for_timeout(300)
                purchase.click(timeout=4000)
            except Exception as e:
                ok = False
                step("reveal_ERROR", error=str(e)[:200])
            page.wait_for_timeout(2000)
            step("reveal_purchase", clicked=ok, **_dom(page))
            _shot(page, "diag_reveal")

            # 6) select seats
            selected = []
            for label in SHOW["seats"]:
                try:
                    # try a few strategies
                    clicked = False
                    for sel in [
                        lambda: page.get_by_text(label, exact=True).first,
                        lambda: page.locator(f'a:has-text("{label}")').first,
                        lambda: page.locator(f'[data-seat="{label}"]').first,
                    ]:
                        try:
                            loc = sel()
                            loc.wait_for(state="visible", timeout=4000)
                            loc.click(timeout=3000)
                            clicked = True
                            break
                        except Exception:
                            continue
                    selected.append({"label": label, "clicked": clicked})
                    page.wait_for_timeout(400)
                except Exception as e:
                    selected.append({"label": label, "error": str(e)[:120]})
            page.wait_for_timeout(1500)
            step("select_seats", attempts=selected, **_dom(page))
            _shot(page, "diag_seats")

            # 7) fill form
            form = {}
            for field, val in [("customer_name", SHOW["name"]), ("msisdn", SHOW["phone"]), ("msisdn_confirm", SHOW["phone"])]:
                try:
                    page.fill(f"input[name={field}]", val)
                    form[field] = "ok"
                except Exception as e:
                    form[field] = f"FAIL: {str(e)[:80]}"
            try:
                t = page.locator("input[name=terms]").first
                if not t.is_checked():
                    t.check(force=True)
                form["terms"] = "ok"
            except Exception as e:
                form["terms"] = f"FAIL: {str(e)[:80]}"
            step("fill_form", fields=form, **_dom(page))
            _shot(page, "diag_form")

            step("STOP_before_buy", note="no purchase clicked; nothing booked")

        except Exception as e:
            step("FLOW_ERROR", error=f"{type(e).__name__}: {str(e)[:300]}", **_dom(page))
            _shot(page, "diag_error")
        finally:
            page.wait_for_timeout(3000)
            browser.close()

    print("\n=== DIAG REPORT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
