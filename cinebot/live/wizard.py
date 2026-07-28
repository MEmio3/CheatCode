"""Drive the booking wizard one step at a time and dump state after each.

Wizard: Location -> Show Date -> Hall -> Show Time -> seat map. Text-based
clicks (robust to class churn). Exploratory: prints a transcript so we can lock
selectors for the real run.
"""
from __future__ import annotations

import json

from .auth import _UA, ORIGIN


def step(page, label: str) -> None:
    print(f"\n--- after {label} ---  url={page.url}")
    try:
        affs = page.eval_on_selector_all(
            "a, button, li, [role=button], label, span, [class*=show], [class*=hall], [class*=date], [class*=seat], [class*=time]",
            """els => els.map(e => ({tag:e.tagName, text:(e.textContent||'').trim().slice(0,40),
              cls:(e.className||'').toString().slice(0,60)})).filter(x => x.text).slice(0,45)""",
        )
        for a in affs:
            print("  ", json.dumps(a, ensure_ascii=False))
    except Exception as e:
        print("  dump err:", e)


def click_first(page, candidates: list[str], *, timeout: int = 2500) -> bool:
    for txt in candidates:
        try:
            page.locator(f"text={txt}").first.click(timeout=timeout)
            print(f"  >> clicked {txt!r}")
            return True
        except Exception:
            continue
    print(f"  >> none of {candidates} matched")
    return False


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_context(user_agent=_UA, viewport={"width": 1320, "height": 900}).new_page()
        page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
        try:
            page.click("button.guest-login")
            page.wait_for_timeout(2500)
        except Exception as e:
            print("guest login failed:", e)
        step(page, "guest login")

        # 1) location
        try:
            page.locator("a").filter(has_text="Bashundhara Shopping Mall").first.click(timeout=6000)
            page.wait_for_timeout(1500)
        except Exception as e:
            print("location click err:", e)
        step(page, "pick Bashundhara location")

        # 2) date (tomorrow). Try several textual forms.
        click_first(page, ["Wed 29", "29 Jul", "Jul 29", "29-Jul", "2026-07-29", "29"])
        page.wait_for_timeout(1500)
        step(page, "pick date")

        # 3) hall 6
        click_first(page, ["Hall 6", "HALL 6", "hall 6"])
        page.wait_for_timeout(1500)
        step(page, "pick Hall 6")

        # 4) showtime 20:00 (Evil Dead Burn)
        click_first(page, ["Evil Dead Burn", "20:00", "20 PM"])
        page.wait_for_timeout(2500)
        step(page, "pick showtime")

        # seat-map probe
        try:
            n = page.eval_on_selector_all("[class*=seat]", "els => els.length")
            print(f"\nseat-like elements on page: {n}")
        except Exception:
            pass

        page.wait_for_timeout(3000)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
