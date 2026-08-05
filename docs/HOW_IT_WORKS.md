# How CineBot works

CineBot books STAR Cineplex (Bangladesh) tickets and pays via bKash using a
**browser-driven flow**. This doc explains what the bot does, where the browser
is used, and how the OTP/PIN step works.

> Status: the browser flow is the supported path. A direct-API booking
> experiment (`api_booking.py`, the "Test API booking" button) was explored and
> **shelved** — `/booking` and `/purchase` work over HTTP, but payment
> (SSL Commerz → bKash) cannot, so the complexity wasn't worth it. See
> "Shelved: direct-API booking" at the bottom.

## The payment chain (the real end-to-end flow)

Confirmed from a captured run (`booking_id` 10080426095208):

1. `POST /api/v1/booking` → `{ "status":"success", "code":200,
   "data":{"booking_id":"..."}, "message":["Booking success."] }`. **Holds the
   seats.** Body carries movie/hall/program/seat IDs, attendee phone, and a
   `recaptcha_token`.
2. `POST /api/v1/purchase` → `{ "data":{"url":"https://epay-gw.sslcommerz.com/<session>"}, ... }`.
3. Browser opens the SSL Commerz URL → the hosted payment page.
4. Selecting bKash → `https://payment.bkash.com/?paymentId=...`.
5. You enter OTP (from SMS) then PIN → confirms → redirect back to a Cineplex
   "success" page.

**Steps 3–5 MUST happen in a real browser** — SSL Commerz and bKash are hosted
pages with their own JS; raw HTTP cannot render them. **reCAPTCHA cannot be
bypassed**; only a real browser can mint the token used in step 1.

## The browser flow (the supported path)

Started by **"Verify seats & launch payments"** (`POST /api/group/start`). One
Chrome window per payment session; everything automated **except** you supply
the OTP/PIN values.

Per session, the bot's browser:
1. Guest-login (reCAPTCHA scored naturally inside the real session).
2. Navigates: location → date → movie → hall/showtime.
3. Picks the seat class, sets quantity, clicks the exact seats.
4. Fills attendee name + bKash number, ticks the terms box.
5. Waits for all sessions to be "ready", then clicks **Purchase** (serialized —
   only one `/booking` in flight at a time) → seats held.
6. Clicks Mobile Banking → bKash → fills the wallet number → Confirm.
7. Reaches the bKash OTP page and **asks you for the OTP** via a modal on the
   local control page.

### How OTP + PIN work

- The bot reaches the bKash OTP page and **pauses**. A modal pops on your local
  CineBot page asking for the 6-digit OTP bKash just SMS'd.
- You type it → the bot types it into bKash and clicks Confirm.
- If bKash asks for the PIN, the modal reopens for the 5-digit PIN → bot types
  it and confirms.
- The bot watches the window for the Cineplex "success" page.

OTP and PIN **always come from the human**; the bot relays them into the bKash
page. If auto-fill stalls (Confirm button not enabling), the session **parks**
as *"Enter OTP/PIN manually in the browser window"* and keeps watching — finish
it by hand in the (visible) Chrome window and the bot still detects success.

## Browser mode (headless toggle)

- `CINEBOT_HEADLESS` env var (default `true`) or the **"Browser Mode"** button.
- **Headless** = Chrome runs invisibly (good for an always-on sniper).
- **Visible** = Chrome windows appear on screen. More reliable for booking +
  payment, and required if you need to finish OTP/PIN manually.
- The mode is read when a run launches, so set it **before** starting.

## Where things live

- `cinebot/ui/app_v2.py` — FastAPI server + endpoints.
- `cinebot/live/group_booking.py` — the browser flow (incl. `_drive_ssl_payment`,
  the shared SSL Commerz → bKash tail).
- `cinebot/live/catalog.py` — read-only catalog (locations/dates/shows/seats) over HTTP.
- `cinebot/sniper.py` — schedule watcher that fires the browser flow when a target drops.
- `docs/api-map.md` — the observed Cineplex API surface + recon notes.

## Shelved: direct-API booking

`/booking` (returns `booking_id`) and `/purchase` (returns the SSL Commerz URL)
were proven to work over pure HTTP using a browser-harvested reCAPTCHA token.
The experiment was shelved because the payment leg still needs a browser, so the
speed-up didn't remove the hardest part. The code was removed; this section
exists so a future attempt knows what's already mapped.
