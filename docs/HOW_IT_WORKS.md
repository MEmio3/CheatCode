# How CineBot works

CineBot books STAR Cineplex (Bangladesh) tickets and pays via bKash. There are
**two booking paths**. This doc explains both, where the browser vs the API is
used, and exactly how the OTP/PIN step works.

## The payment chain (the real end-to-end flow)

Confirmed from a captured run (`booking_id` 10080426095208, 2026-08-04):

1. `POST /api/v1/booking` → `{ "status":"success", "code":200,
   "data":{"booking_id":"10080426095208"}, "message":["Booking success."] }`.
   This **holds the seats**. The request body carries `movie_id`, `hall_id`,
   `loc_id`, `schedule_id`, `seatTypeId`, `seat_no` (seat sequence IDs),
   `seat_name` (labels), attendee `msisdn`, and a `recaptcha_token`.
2. `POST /api/v1/purchase` → hands off to SSL Commerz.
3. Browser opens `https://epay-gw.sslcommerz.com/<session-hash>` — the hosted
   SSL Commerz payment page.
4. Selecting bKash → `https://epay-comm.sslcommerz.com/api.php/bkash/init-transaction`
   → `https://payment.bkash.com/?paymentId=...` (the bKash page).
5. You enter OTP (from SMS) then PIN → payment confirms → redirect back to a
   Cineplex "success" page.

**Steps 3–5 MUST happen in a real browser.** SSL Commerz and bKash are hosted
pages with their own JS; raw HTTP cannot render them — this is exactly why a
pure-API attempt hangs on a spinner. **reCAPTCHA cannot be bypassed either**;
only a real browser can mint the token used in step 1.

---

## Path A — Browser flow (the default, fully working except the final OTP/PIN click)

Started by the **"Verify seats & launch payments"** button
(`POST /api/group/start`). One Chrome window per payment session; everything is
automated **except** you supply the OTP/PIN values.

Per session, the bot's browser:
1. Guest-login (reCAPTCHA scored naturally inside the real session).
2. Navigates: location → date → movie → hall/showtime.
3. Picks the seat class, sets quantity, clicks the exact seats.
4. Fills attendee name + bKash number, ticks the terms box.
5. Waits for all sessions to be "ready", then clicks **Purchase** (serialized —
   only one `/booking` in flight at a time) → seats held.
6. Clicks Mobile Banking → bKash → fills the wallet number → Confirm.
7. Reaches the bKash OTP page and **asks you for the OTP** through a modal on
   the local control page.

### How OTP + PIN work (the part that's confusing)

- The bot reaches the bKash OTP page and **pauses**. A modal pops on your local
  CineBot page asking for the 6-digit OTP that bKash just SMS'd to the
  attendee's number.
- You type the OTP into that modal → the bot types it into the bKash page and
  clicks Confirm.
- If bKash then asks for the account PIN, the modal reopens for the 5-digit PIN
  → the bot types it and confirms.
- The bot then watches the window for the Cineplex "success" page.

So the OTP and PIN **always come from the human** (the SMS + the attendee's PIN);
the bot just relays them into the bKash page so you don't have to chase each
window. In **visible** mode you can also finish OTP/PIN directly in the browser
window if the bot stalls. In **headless** mode the modal is the only way in.

This path is reliable but **slow** (~40 s to the OTP prompt): every session does
its own guest-login + navigation + seat clicking.

> Known issue: after typing the OTP, the bKash "Confirm" button sometimes stays
> disabled and the run errors with *"did not enable Purchase in time"*. This is
> a fragility in the OTP auto-fill, not the booking. Until it's hardened, use
> **Visible** mode and finish OTP/PIN in the browser window if it stalls.

---

## Path B — Direct-API booking (the speed-up; currently a probe)

Started by the **"Test API booking"** button (`POST /api/group/api-probe`).
Goal: skip seat-clicking/navigation for the **booking** leg only.

What the probe does today (booking only, **no payment**):
1. Opens ONE browser, clicks Guest Login, and mints a reCAPTCHA v3 token via
   `grecaptcha.execute(siteKey, {action:'booking'})`.
2. `POST /api/v1/booking` over HTTP (curl-cffi) with the captured body shape +
   the harvested token.
3. Prints the raw response so we can confirm `/booking` accepts a harvested
   token, and see the response shape.

A successful probe **holds real seats** (it's a real booking) but does not pay.

### Planned full fast path (now that the chain is mapped)

1. **API** — guest-login once + `/booking` × N over HTTP using harvested tokens.
   No seat clicking, no per-session logins.
2. **API** — `/purchase` × N → each returns its SSL Commerz session URL
   (`https://epay-gw.sslcommerz.com/<session>`).
3. **Browser** — `goto` that URL per session → it redirects to bKash → OTP/PIN
   (the only browser step left).

This removes the entire seat-selection layer — the slow, fragile part that has
caused most failures — and keeps a browser only for the bKash payment, which
genuinely needs one. Expected time-to-OTP: a few seconds instead of ~40.

---

## Browser mode (headless toggle)

- `CINEBOT_HEADLESS` env var (default `true`) or the **"Browser Mode"** button.
- **Headless** = Chrome runs invisibly (good for an always-on sniper watching in
  the background).
- **Visible** = Chrome windows appear on screen.
- Booking/seat-selection is most reliable in **Visible** mode. The mode is read
  when a run launches, so set it **before** starting a run.

## Where things live

- `cinebot/ui/app_v2.py` — FastAPI server; the two endpoints above.
- `cinebot/live/group_booking.py` — Path A (browser flow).
- `cinebot/live/api_booking.py` — Path B (direct-API probe).
- `cinebot/live/catalog.py` — read-only catalog (locations/dates/shows/seats) over HTTP.
- `cinebot/sniper.py` — schedule watcher that fires Path A when a target drops.
- `docs/api-map.md` — the observed Cineplex API surface + recon notes.
