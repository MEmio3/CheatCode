"""Explore the live SPA after guest login to map the booking UI.

Logs in, lands on /home, then reports the DOM affordances and tries to click into
the target movie so we can learn the route pattern + selectors for seat
selection. Observer-leaning: it prints a transcript and stops early.
"""
from __future__ import annotations

import json

from .auth import guest_token_via_browser, _UA, ORIGIN


def dump(page, label: str) -> None:
    print(f"\n=== {label} ===")
    print("url:", page.url)
    try:
        affs = page.eval_on_selector_all(
            "a, button, [role=button], [class*=movie], [class*=show], [class*=book]",
            """els => els.map(e => ({
              tag:e.tagName, text:(e.textContent||'').trim().slice(0,60),
              id:e.id||null, cls:(e.className||'').toString().slice(0,80),
              href:e.getAttribute('href')||null
            })).filter(x => x.text||x.href).slice(0,40)""",
        )
        for a in affs:
            print("  ", json.dumps(a, ensure_ascii=False))
    except Exception as e:
        print("  dump failed:", e)


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 850})
        page = ctx.new_page()

        # log in as guest
        page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
        try:
            page.click("button.guest-login")
            page.wait_for_timeout(2500)
        except Exception as e:
            print("guest click failed:", e)

        dump(page, "/home after guest login")

        # try to find and click the target movie card
        try:
            target = page.eval_on_selector_all(
                "*",
                """els => {
                  const out=[];
                  for (const e of els) {
                    const t=(e.textContent||'').trim();
                    if (t.includes('Evil Dead') && t.length < 80) {
                      out.push({tag:e.tagName, cls:(e.className||'').toString().slice(0,80),
                                id:e.id||null, text:t.slice(0,60)});
                      if (out.length>10) break;
                    }
                  }
                  return out;
                }""",
            )
            print(f"\nEvil Dead candidates: {len(target)}")
            for t in target[:10]:
                print("  ", json.dumps(t, ensure_ascii=False))
        except Exception as e:
            print("search failed:", e)

        page.wait_for_timeout(3000)
        ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
