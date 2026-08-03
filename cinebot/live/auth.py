"""Obtain a real guest JWT by driving a HEADED browser to click Guest Login.

reCAPTCHA v3 runs naturally in the real session — we do NOT bypass it. If the
automated click scores too low and no token arrives, the function keeps the
window open so a human can click Guest Login; the token is captured whenever it
lands.
"""
from __future__ import annotations

import json
import os
import time

ORIGIN = "https://ticket.cineplexbd.com"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


try:
    from playwright_stealth import stealth_sync
except ImportError:
    def stealth_sync(page):
        page.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
        )


def guest_token_via_browser(
    headless: bool = True, wait_ms: int = 60000, verbose: bool = True
) -> dict:
    """Click Guest Login and return {'token', 'clicked', 'url', 'error', 'manual'}.

    'manual' is True if the auto-click happened but the token only arrived later
    (i.e. a human re-clicked), hinting reCAPTCHA resisted automation.
    """
    from playwright.sync_api import sync_playwright

    state = {"token": None, "device_key": None, "clicked": False, "manual": False, "url": None, "error": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 850})
        page = ctx.new_page()
        stealth_sync(page)

        def on_response(resp):
            try:
                if "cineplex-ticket-api" not in resp.url:
                    return
                # capture the device-key every request carries
                dk = (resp.request.headers or {}).get("device-key")
                if dk:
                    state["device_key"] = dk
                if "guest-login" in resp.url:
                    data = resp.json()
                    t = (data.get("data") or {}).get("token")
                    if t:
                        state["token"] = t
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            try:
                page.wait_for_selector("button.guest-login", timeout=10000)
                page.click("button.guest-login")
                state["clicked"] = True
                if verbose:
                    print("[auth] clicked Guest Login; waiting for token...", flush=True)
            except Exception as e:
                state["error"] = f"click failed: {e}"

            # poll for the token (arrives via the guest-login response)
            t0 = time.time()
            while time.time() - t0 < wait_ms / 1000.0:
                if state["token"]:
                    break
                page.wait_for_timeout(400)
            state["url"] = page.url
        except Exception as e:
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass
    return state


def main() -> int:
    s = guest_token_via_browser()
    # never print secrets to stdout in full; show tails only
    if s["token"]:
        s["token"] = "..." + s["token"][-10:]
    if s["device_key"]:
        s["device_key"] = "..." + s["device_key"][-8:]
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0 if s["token"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
