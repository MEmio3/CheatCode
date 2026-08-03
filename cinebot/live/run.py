"""Full real booking run, driven in a headed browser on the user's PC.

Flow: guest login -> Bashundhara -> 29 Jul -> Evil Dead Burn -> Hall 6 8:00 PM
-> Premium -> qty 2 -> select J10/J11 -> fill guest form -> Purchase Ticket ->
payment gateway -> choose bKash -> enter the bKash number -> STOP at the OTP
field. The user reads the OTP from their phone.

Hard stops where a human must act:
  - guest-login reCAPTCHA (handled automatically so far)
  - the bKash OTP + PIN (NEVER entered by this code)

Reads the bKash number from $CINEBOT_BKASH (never written into source).
"""
from __future__ import annotations

import json
import os
import sys

from .auth import _UA, ORIGIN


def _click(page, *selectors, timeout=5000):
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _click_when_enabled(page, selector: str, *, timeout_ms: int = 30000, label: str = "") -> bool:
    """Wait for a button to lose [disabled] (the Purchase button stays disabled
    until the invisible reCAPTCHA v3 resolves), then click it."""
    import time as _t

    loc = page.locator(selector).first
    deadline = _t.time() + timeout_ms / 1000.0
    while _t.time() < deadline:
        _dismiss_swal2(page)
        try:
            disabled = loc.get_attribute("disabled")
            if disabled is None and loc.is_visible():
                loc.click(timeout=4000)
                print(f"  [click] {label or selector} (enabled after wait)")
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    print(f"  [click] {label or selector} never became enabled")
    return False


def _dismiss_swal2(page) -> None:
    """Dismiss SweetAlert2 popups that intercept pointer events (validation errors)."""
    try:
        btn = page.locator(".swal2-confirm").first
        if btn.is_visible(timeout=300):
            btn.click(timeout=1000)
    except Exception:
        pass


def _shot(page, name: str) -> None:
    try:
        page.screenshot(path=f"E:/1-Ticket/{name}.png", full_page=True)
        print(f"  [shot] E:/1-Ticket/{name}.png")
    except Exception:
        pass


def _dump(page, label: str) -> None:
    print(f"\n--- {label} ---  url={page.url}")
    try:
        info = page.evaluate(
            """() => ({
              heads: [...new Set([...document.querySelectorAll('h1,h2,h3,h4,label')].map(e=>(e.textContent||'').trim().slice(0,40)).filter(Boolean))].slice(0,25),
              inputs: [...document.querySelectorAll('input,select')].map(e=>({name:e.name||e.id||null, type:e.type||null, ph:e.placeholder||null})).slice(0,15),
              btns: [...document.querySelectorAll('button,[role=button],.btn,a.btn')].map(e=>(e.textContent||'').trim().slice(0,30)).filter(Boolean).slice(0,20),
              body: (document.body.innerText||'').slice(0,700)
            })"""
        )
        print("  heads:", info["heads"])
        print("  inputs:", json.dumps(info["inputs"], ensure_ascii=False))
        print("  btns:", info["btns"])
        body = info["body"]
        try:
            print("  body:", body.encode("ascii", "ignore").decode()[:500])
        except Exception:
            print("  body: <unreadable>")
    except Exception as e:
        print("  dump err:", e)


def main() -> int:
    bkash = os.environ.get("CINEBOT_BKASH", "").strip()
    name = os.environ.get("CINEBOT_NAME", "Guest User").strip()
    if not bkash:
        print("set CINEBOT_BKASH=<your bKash number> env var", file=sys.stderr)
        return 2
    print(f"[run] name={name!r} bkash=...{bkash[-4:]}")

    from playwright.sync_api import sync_playwright
    import os
    headless_flag = os.getenv("CINEBOT_HEADLESS", "true").lower() not in ("false", "0", "no")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless_flag)
        page = browser.new_context(user_agent=_UA, viewport={"width": 1320, "height": 900}).new_page()

        # capture token + device-key during navigation so we can fetch live seats
        sess = {"token": None, "device_key": None}

        def _on_resp(resp):
            try:
                if "cineplex-ticket-api" not in resp.url:
                    return
                dk = (resp.request.headers or {}).get("device-key")
                if dk:
                    sess["device_key"] = dk
                if "guest-login" in resp.url:
                    t = (resp.json().get("data") or {}).get("token")
                    if t:
                        sess["token"] = t
            except Exception:
                pass

        page.on("response", _on_resp)

        # 1) guest login
        page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
        page.click("button.guest-login")
        page.wait_for_timeout(2500)
        print("[1] guest login done")

        # 1b) pick the best AVAILABLE 2 seats dynamically, EXCLUDING ones already
        # held by prior runs (the get-seat API doesn't reflect transient holds, so
        # we hard-exclude known-held labels to avoid booking-conflict warnings).
        seats = ["H10", "H11"]  # fallback
        held = {"J10", "J11", "H10", "H11"}  # held by earlier attempts in this session
        try:
            from dataclasses import replace as _replace
            from ..browse import CineplexClient
            from ..seats.scorer import find_best_block
            if sess["token"] and sess["device_key"]:
                c = CineplexClient(device_key=sess["device_key"], token=sess["token"])
                sm = CineplexClient.raw_seats_to_seatmap(c.get_seat_layout(1, 129692))
                free = [_replace(s, available=False) if f"{s.row_label}{s.col_label}" in held else s for s in sm.seats]
                from ..seats.scorer import SeatMap as _SM
                sm = _SM(n_rows=sm.n_rows, n_cols=sm.n_cols, seats=free)
                block = find_best_block(sm, 2)
                if block:
                    seats = [f"{s.row_label}{s.col_label}" for s in block]
            print(f"[1b] dynamic best-available seats (excluding held {sorted(held)}): {seats}")
        except Exception as e:
            print(f"[1b] dynamic pick failed, using fallback {seats}: {e}")

        # 2) wizard
        page.locator("a").filter(has_text="Bashundhara Shopping Mall").first.click(timeout=8000)
        page.wait_for_timeout(1500)
        page.locator("text=29 Jul").first.click(timeout=5000)
        page.wait_for_timeout(1500)
        page.locator("text=Evil Dead Burn").first.click(timeout=5000)
        page.wait_for_timeout(2000)
        page.locator("div.card-wrap.ticket-time").filter(has_text="Hall 6").locator("a").first.click(timeout=6000)
        page.wait_for_timeout(2500)
        print("[2] showtime Hall 6 8:00 PM selected")

        # 3) seat type Premium
        page.locator(".ticket_booking_left_item.seat_type").first.click(timeout=5000)
        page.wait_for_timeout(600)
        page.locator(".seat_type_select").get_by_text("Premium", exact=False).first.click(timeout=5000)
        page.wait_for_timeout(1500)
        print("[3] seat type Premium")

        # 4) quantity 2
        plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
        plus.click(timeout=4000); page.wait_for_timeout(400)
        plus.click(timeout=4000); page.wait_for_timeout(1500)
        print("[4] qty 2")

        # 5) reveal seats + pick the dynamic best-available pair
        _click_when_enabled(page, "button.btn-desktop-purchase", timeout_ms=30000, label="Purchase (reveal seats)")
        page.wait_for_timeout(2500)
        for label in seats:
            _dismiss_swal2(page)
            try:
                page.get_by_text(label, exact=True).first.click(timeout=5000)
                page.wait_for_timeout(500)
            except Exception as e:
                print(f"  seat {label} click issue: {e}")
        page.wait_for_timeout(1500)
        try:
            print("[5] selected seats:", page.locator(".selected_seat").inner_text(timeout=2000).replace("\n", " "))
        except Exception:
            pass

        # 6) guest checkout form
        try:
            page.fill("input[name=customer_name]", name)
            page.fill("input[name=msisdn]", bkash)
            page.fill("input[name=msisdn_confirm]", bkash)
            try:
                page.check("input[name=terms]")
            except Exception:
                page.locator("input[name=terms]").first.click(force=True)
            print("[6] guest form filled")
        except Exception as e:
            print("[6] form fill issue:", e)

        _shot(page, "10_before_purchase")
        # 7) PURCHASE -> real hold + order + redirect to payment gateway
        print("[7] clicking Purchase Ticket (creates real hold + order)...")
        _click_when_enabled(page, "button.btn-desktop-purchase", timeout_ms=30000, label="Purchase (buy)")
        page.wait_for_timeout(6000)
        _dump(page, "after Purchase Ticket")
        _shot(page, "11_after_purchase")

        # 8) payment gateway: Mobile Banking tab -> bKash -> number -> PAY
        # wait for the SSL Commerz gateway UI to render
        try:
            page.get_by_text("MOBILE BANKING", exact=False).first.wait_for(timeout=20000)
        except Exception:
            pass
        # click the Mobile Banking tab
        for sel in ["text=MOBILE BANKING", "text=Mobile Banking"]:
            try:
                page.locator(sel).first.click(timeout=4000)
                print("[8] clicked Mobile Banking tab")
                break
            except Exception:
                continue
        page.wait_for_timeout(4500)
        # dump wallet options so we can target bKash (logos are often CSS bg images)
        try:
            wallets = page.evaluate(
                """() => {
                  const out=[];
                  document.querySelectorAll('button, a, [role=button], li, div, img, label').forEach(e => {
                    const html=(e.outerHTML||'').toLowerCase();
                    const style = (e.getAttribute('style')||'').toLowerCase();
                    if ((html.includes('bkash') || style.includes('bkash')) && e.offsetParent!==null && e.children.length<4) {
                      out.push({tag:e.tagName, cls:(e.className||'').toString().slice(0,40),
                                style:style.slice(0,60), text:(e.textContent||'').trim().slice(0,20)});
                    }
                  });
                  return [...new Set(out.map(o=>JSON.stringify(o)))].map(JSON.parse).slice(0,12);
                }"""
            )
            print("  wallet options:", json.dumps(wallets, ensure_ascii=False))
        except Exception as e:
            print("  wallet dump err:", e)

        # click bKash (match text, alt, src, class, OR style/background-image)
        for sel in ["text=bKash", "text=Bkash", "[alt*=bkash]", "[alt*=Bkash]",
                    "img[src*=bkash]", "[class*=bkash]", "[class*=Bkash]",
                    "div[style*=bkash]", "a[style*=bkash]", "button[style*=bkash]"]:
            try:
                page.locator(sel).first.click(timeout=2500)
                print(f"[8] selected bKash via {sel}")
                break
            except Exception:
                continue
        page.wait_for_timeout(4500)
        _shot(page, "12_bkash_form")

        # 9) bKash payment page: fill wallet number + Confirm -> OTP fires
        try:
            wallet = page.locator(
                'input[name="WALLET"], input[name="wallet"], input[name*="WALLET"], '
                "input[placeholder*='01X'], input[type=tel], input[name*=mobile]"
            ).first
            wallet.wait_for(timeout=15000)
            wallet.fill(bkash)
            print(f"[9] entered bKash wallet ...{bkash[-4:]} on payment.bkash.com")
            page.wait_for_timeout(800)
            # click Confirm (prefer a real button; avoid the "Confirm and proceed" label text)
            for sel in ["button:has-text('Confirm')", "[role=button]:has-text('Confirm')",
                        "a:has-text('Confirm')", "input[value=Confirm]"]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        loc.last.click(timeout=3000)
                        print(f"[9] clicked {sel}")
                        break
                except Exception:
                    continue
            page.wait_for_timeout(8000)
        except Exception as e:
            print("[9] bKash wallet/confirm issue:", e)
        _dump(page, "after bKash Confirm (OTP page?)")
        _shot(page, "13_otp_stage")

        _dump(page, "final — looking for OTP field")
        _shot(page, "13_otp_stage")
        print("\n*** STOPPING at payment. If a bKash OTP field/page is shown, the OTP "
              "stage has been reached. Check your phone for the bKash SMS. ***")
        # leave the window up so the user can see the OTP page
        page.wait_for_timeout(60000)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
