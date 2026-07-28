"""Real, headed-browser flow driver for the Cineplex booking site.

This is the live counterpart to cinebot.flow.MockFlow. It drives a REAL browser
on the user's PC (BD-reachable) through the actual SPA, so reCAPTCHA v3 scores a
natural session (we do NOT bypass it) and the bKash payment gateway — which is a
redirect/iframe mess — works the way it does for a human.

Build order:
  1. auth.guest_token_via_browser()  — click Guest Login, capture the JWT
  2. browse chain over HTTP (token in hand) — locations/showdates/shows/seat
  3. hold + order + payment handoff in the browser (gateway needs it)
  4. STOP at the bKash OTP field — the user reads the OTP from their phone

No bKash PIN is ever entered by this code. The user authorizes with their own
OTP at the gateway. Reaching the OTP field is the defined end of automation.
"""
from __future__ import annotations

import json
import sys

ORIGIN = "https://ticket.cineplexbd.com"


def probe(timeout_ms: int = 45000, headless: bool = False) -> dict:
    """Open the live site in a headed browser, capture any guest-login token
    passively, and dump the top-level DOM affordances so we can map selectors.

    Observer-only on this pass: it does not click anything. It tells us whether
    the SPA auto-issues a guest token on load and what buttons/links exist.
    """
    from playwright.sync_api import sync_playwright

    out: dict = {"guest_token": None, "recaptcha_sitekey": None, "affordances": [], "url_after_load": None, "errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 850},
        )
        page = ctx.new_page()

        def on_response(resp):
            try:
                if "guest-login" in resp.url:
                    data = resp.json()
                    t = (data.get("data") or {}).get("token")
                    if t:
                        out["guest_token"] = t
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            out["url_after_load"] = page.url

            # reCAPTCHA site key (if the script is already injected)
            try:
                out["recaptcha_sitekey"] = page.evaluate(
                    """() => {
                      const s = document.querySelector('script[src*="recaptcha"]');
                      const m = s && s.src.match(/render=([^&]+)/);
                      const g = document.querySelector('.g-recaptcha');
                      return (m && m[1]) || (g && g.getAttribute('data-sitekey')) || null;
                    }"""
                )
            except Exception as e:
                out["errors"].append(f"recaptcha-probe: {e}")

            # dump clickable affordances (learn selectors for movie/login nav)
            out["affordances"] = page.eval_on_selector_all(
                "a, button, [role=button]",
                """els => els.map(e => ({
                  tag: e.tagName, text: (e.textContent||'').trim().slice(0, 48),
                  id: e.id || null, cls: (e.className||'').toString().slice(0, 60),
                  href: e.getAttribute('href') || null
                })).filter(x => x.text || x.id || x.href).slice(0, 80)""",
            )
        except Exception as e:
            out["errors"].append(f"{type(e).__name__}: {e}")
        finally:
            # keep the window up briefly so a human can see it, then close
            try:
                page.wait_for_timeout(min(timeout_ms, 4000))
            except Exception:
                pass
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass
    return out


def main() -> int:
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
