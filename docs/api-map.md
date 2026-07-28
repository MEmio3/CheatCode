# API Map — ticket.cineplexbd.com

Filled by passive recon (reading the site's JS bundle + probing endpoints). Active
recon (`python -m cinebot.recon.capture`) confirms the request bodies + the
seat-hold TTL.

> **Status:** Partial — base URL, endpoints, auth model, and gating are CONFIRMED
> by source. Exact request bodies + whether reCAPTCHA is enforced per-endpoint
> are confirmed by a 2-min active recon (browse a showtime with DevTools open).

## Base + gating (CONFIRMED)

- **Base URL:** `https://cineplex-ticket-api.cineplexbd.com/api/v1`
- **Transport:** HTTPS, JSON bodies, all calls appear to be `POST`.
- **Required on every request (headers):**
  - `appsource: web`  (literal constant from the bundle)
  - `device-key: <uuid>`  (client-generated, persisted in localStorage)
  - `Content-Type: application/json`
- **Validation gate:** without `appsource` + `device-key`, server returns
  `{"status":"error","code":422,"message":["The appsource field is required.","The device-key field is required."]}`.
  With them, requests advance (so these are the only universal gate).
- **Error shape:** Laravel-style JSON (`status`, `code`, `data`, `message[]`).

## Auth model (CONFIRMED from source)

- **Scheme:** JWT Bearer. Interceptor adds `Authorization: Bearer ${token}` to
  every request, where `token` comes from `localStorage["userInfo"].token`.
- **Token issued by:** `/login` (account) or `/guest-login` (guest).
- **Guard:** requests are cancelled if no token AND the endpoint isn't
  `login`/`guest-login` — i.e. guest-login is the account-less entry.
- **Open question (active recon):** do the read endpoints (`get-shows`,
  `get-seat`) require a Bearer token, or only `appsource`+`device-key`?
  Browsing the site as a guest answers this.

## Endpoints (CONFIRMED names from bundle)

Read/browse:
- `get-location` — branches/theatres
- `get-showdate` — available dates
- `get-shows` — movies + showtimes (returns `show_time_id`, `movie_id`, `hallId`, `screen_name`)
- `get-seat` — **the seat map** for a `show_time_id` (the one we want)

Booking/payment:
- `booking` — create booking from selected seats
- `bookingCheck` — validate/refresh a booking (likely the seat-hold lifecycle)
- `purchase` — initiate purchase
- `verify` — SSL Commerz payment verification
- `ssl` — SSL Commerz handoff (redirect/iframe)

Auth:
- `guest-login`, `login`, `register`, `logout`
- `send-otp`, `otp-verify`, `otp-resend`
- `change-password`, `reset-password`

## Seat data model (field names from bundle)

`show_time_id`, `movie_id`, `hallId`, `screen_name`, `hall_name`, `seatclass`,
`seatclassName`, `seat_type`, `layout`, `selected_seat`, `booking_no`.

## Anti-bot (CONFIRMED present)

- **reCAPTCHA v3** loaded on the page (invisible, score-based).
  Site key: `6LchFI8qAAAAAO1tzM3d1sI2TFOzmRmd55G0BoX8`.
- Likely required for `login`/`guest-login`/`booking` (token sent in body,
  validated server-side). We do NOT bypass it — a real browser session
  (Playwright) lets the script score naturally; if it challenges, the human
  solves it.

## Seat-hold lifecycle (Priority #1 — needs active recon)

TBD — captured by watching `bookingCheck`/`booking` responses while a real
showtime's seats are held. This TTL decides hold-then-pay vs pre-authorize-then-hold.

## HTTP vs browser split (decision)

- **Read legs (`get-*`):** likely HTTP-viable (curl-cffi) once `device-key` +
  `appsource` + (if required) Bearer token are set. Confirmed reachable.
- **`guest-login`:** reCAPTCHA v3 → drive via Playwright (real browser) so the
  score is legitimate; capture the token; reuse it for HTTP read legs.
- **`booking`/`purchase`/`ssl` (payment):** Playwright headed — gateway is
  redirect/iframe/JS-heavy and reCAPTCHA-gated.

## Concrete fetch plan (the answer to "how do we fetch seat data")

1. `guest-login` via Playwright (browser runs reCAPTCHA, gets JWT + device-key).
2. `get-location` → pick branch.
3. `get-showdate` → pick date.
4. `get-shows` → find the movie → grab its `show_time_id`.
5. `get-seat(show_time_id)` → real seat-map JSON → feed into `SeatMap`/scorer;
   the DEMO tag comes off.
