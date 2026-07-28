"""Drive the wizard to the seat map and dump seat-cell structure.

Goes: guest login -> Bashundhara -> 29 Jul -> Evil Dead Burn -> Hall 6 8:00 PM,
then waits for the seat grid and prints how seats are rendered (tag/class/attrs)
plus the elements for J10 / J11. HALTS before any purchase — no hold is created.
"""
from __future__ import annotations

import json

from .auth import _UA, ORIGIN


def dump_center(page) -> None:
    print("\n=== center / controls probe ===")
    try:
        info = page.evaluate(
            """() => {
              const inputs = [...document.querySelectorAll('input,select,textarea')].map(e => ({
                tag:e.tagName, type:e.type||null, name:e.name||null,
                ph:e.placeholder||null, cls:(e.className||'').toString().slice(0,60),
                val:e.value||null}));
              const buttons = [...document.querySelectorAll('button')].map(e => ({
                text:(e.textContent||'').trim().slice(0,40), cls:(e.className||'').toString().slice(0,60)}));
              const typed = [...document.querySelectorAll('[class*=qty],[class*=quantity],[class*=seat-type],[class*=seattype],[class*=type],[class*=stepper],[class*=plus],[class*=minus],[class*=adult],[class*=child],[class*=counter]')].map(e => ({
                tag:e.tagName, cls:(e.className||'').toString().slice(0,60), text:(e.textContent||'').trim().slice(0,40)}));
              // main center column text (what the user sees in the body)
              const main = document.querySelector('.ticket_booking_right, main, #main, .container');
              return {inputs: inputs.slice(0,20), buttons: buttons.slice(0,25), typed,
                      bodyText: (document.body.innerText||'').slice(0, 900)};
            }"""
        )
        print("INPUTS/SELECTS:")
        for i in info["inputs"]:
            print("  ", json.dumps(i, ensure_ascii=False))
        print("BUTTONS:")
        for b in info["buttons"]:
            print("  ", json.dumps(b, ensure_ascii=False))
        print("TYPE/QTY-ish elements:")
        for t in info["typed"]:
            print("  ", json.dumps(t, ensure_ascii=False))
        print("\nBODY TEXT (first 900 chars):\n", info["bodyText"])
    except Exception as e:
        print("center probe failed:", e)


def dump_seats(page) -> None:
    print("\n=== seat map probe ===")
    try:
        info = page.evaluate(
            """() => {
              const iframes = [...document.querySelectorAll('iframe')].map(f => ({src:f.src, w:f.clientWidth, h:f.clientHeight}));
              const counts = {};
              for (const el of document.querySelectorAll('*')) counts[el.tagName] = (counts[el.tagName]||0)+1;
              const seatish = {};
              for (const sel of ['[class*=seat]','[class*=Seat]','[class*=row]','[class*=grid]','[class*=layout]','[class*=map]','[class*=screen]','rect','path','td','canvas']) {
                const n = document.querySelectorAll(sel).length;
                if (n) seatish[sel] = n;
              }
              // biggest containers by direct-child count (likely the seat grid)
              let biggest = [];
              document.querySelectorAll('div, ul, tbody, svg').forEach(e => {
                if (e.children.length > 8) biggest.push({tag:e.tagName, cls:(e.className||'').toString().slice(0,50), n:e.children.length});
              });
              biggest.sort((a,b)=>b.n-a.n);
              return {iframes, topTags: Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,12), seatish, biggest: biggest.slice(0,8)};
            }"""
        )
        print("iframes:", json.dumps(info["iframes"], ensure_ascii=False))
        print("top tags:", info["topTags"])
        print("seat-ish counts:", info["seatish"])
        print("biggest containers (likely seat grid):")
        for b in info["biggest"]:
            print("  ", json.dumps(b, ensure_ascii=False))
    except Exception as e:
        print("probe failed:", e)


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_context(user_agent=_UA, viewport={"width": 1320, "height": 900}).new_page()
        page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
        page.click("button.guest-login")
        page.wait_for_timeout(2500)

        page.locator("a").filter(has_text="Bashundhara Shopping Mall").first.click(timeout=8000)
        page.wait_for_timeout(1500)
        page.locator("text=29 Jul").first.click(timeout=5000)
        page.wait_for_timeout(1500)
        page.locator("text=Evil Dead Burn").first.click(timeout=5000)
        page.wait_for_timeout(2000)
        # Hall 6's 8:00 PM (unique on the page)
        page.locator("div.card-wrap.ticket-time").filter(has_text="Hall 6").locator("a").first.click(timeout=6000)
        print("clicked Hall 6 8:00 PM; waiting for seat grid...")
        page.wait_for_timeout(4000)

        # open the seat-type dropdown and pick Premium
        try:
            page.locator(".ticket_booking_left_item.seat_type").first.click(timeout=5000)
            page.wait_for_timeout(600)
            page.locator(".seat_type_select").get_by_text("Premium", exact=False).first.click(timeout=5000)
            print("selected seat type Premium")
        except Exception as e:
            print("seat-type select err:", e)
        # set ticket quantity to 2 via the + stepper (second img in .ticket_qty_view)
        try:
            plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
            plus.click(timeout=4000); page.wait_for_timeout(400)
            plus.click(timeout=4000); page.wait_for_timeout(2000)
            print("clicked + twice (qty -> 2)")
        except Exception as e:
            print("qty+ click err:", e)
        # dump whatever now appears (seat map?)
        try:
            info = page.evaluate(
                """() => {
                  const root = document.getElementById('select-seat-type');
                  const side = document.querySelector('.selected_seat')?.innerText || null;
                  const seats = document.querySelectorAll('[class*=seat],[class*=Seat]').length;
                  const svgs = [...document.querySelectorAll('svg')].filter(s=>(s.clientWidth||0)>250).map(s=>s.clientWidth+'x'+s.clientHeight);
                  return {htmlTail: root? root.outerHTML.slice(-3000):null, selected_seat: side, seatEls: seats, bigSvgs: svgs};
                }"""
            )
            print("selected_seat sidebar:", info["selected_seat"], "| seat-ish els:", info["seatEls"], "| big svgs:", info["bigSvgs"])
            print("#select-seat-type HTML tail:\n", info["htmlTail"])
        except Exception as e:
            print("dump err:", e)
        # try to advance: Purchase Ticket (should open the seat-selection page now)
        before = page.url
        try:
            page.locator("button.btn-desktop-purchase").click(timeout=5000)
            print("clicked Purchase Ticket")
        except Exception as e:
            print("purchase click err:", e)
        page.wait_for_timeout(2500)
        try:
            info = page.evaluate(
                """() => {
                  // seat anchors: <a> whose text is like J10, A3, etc.
                  const seats = [...document.querySelectorAll('a')].filter(a => /^[A-Z]{1,2}\\d+$/.test((a.textContent||'').trim()));
                  const sample = seats.slice(0,3).map(a => ({text:a.textContent.trim(), cls:a.className, href:a.getAttribute('href'), attrs:[...a.attributes].map(x=>x.name+'='+x.value.slice(0,20)).join(',')}));
                  return {n: seats.length, sample, classes: [...new Set(seats.map(s=>s.className))]};
                }"""
            )
            print(f"\nseat anchors found: {info['n']}; classes={info['classes']}")
            print("sample:", json.dumps(info["sample"], ensure_ascii=False))
        except Exception as e:
            print("seat-anchor dump err:", e)

        # click J10 then J11
        for label in ("J10", "J11"):
            try:
                page.get_by_text(label, exact=True).first.click(timeout=4000)
                print(f"  clicked seat {label}")
                page.wait_for_timeout(500)
            except Exception as e:
                print(f"  click {label} err: {e}")
        page.wait_for_timeout(1500)
        try:
            sel = page.locator(".selected_seat").inner_text(timeout=2000)
            total = page.locator(".total_amount").inner_text(timeout=2000)
            print("sidebar ->", sel, "|", total)
        except Exception:
            pass
        page.screenshot(path="E:/1-Ticket/seatpage.png", full_page=True)
        print("screenshot -> E:/1-Ticket/seatpage.png")
        page.screenshot(path="E:/1-Ticket/seatpage.png", full_page=True)
        print("screenshot -> E:/1-Ticket/seatpage.png")
        # also show the sidebar (seat type / qty / total) to confirm seat page loaded
        try:
            side = {li: page.locator(f"li.{cls}").inner_text(timeout=1000)
                    for li, cls in [("loc", "show_location"), ("date", "show_date"),
                                    ("hall", "hall_name"), ("time", "show_time"),
                                    ("seattype", "seat_type"), ("qty", "ticket_qty"),
                                    ("seat", "selected_seat"), ("total", "total_amount")]}
            print("\nsidebar:", json.dumps(side, ensure_ascii=False, indent=2))
        except Exception as e:
            print("sidebar read failed:", e)

        page.wait_for_timeout(3000)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
