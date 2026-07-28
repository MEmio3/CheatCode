# Hall 6 Group Booker

A focused, local-only STAR Cineplex booking console for this target:

- **Movie:** Spider-Man: Brand New Day
- **Date:** 1 August 2026
- **Location:** Bashundhara Shopping Mall
- **Hall:** Hall 6
- **Time window:** 4:00–6:00 PM
- **Seats:** the complete E and F rows

The captured Hall 6 layout contains 17 seats in E and 17 in F, so the plan is
**34 seats** across four row-local transactions: `10 + 7 + 10 + 7`.

## What the UI asks for

1. One bKash number.
2. Four real attendee names.
3. The matching bKash OTP when each payment reaches that stage.

The number, names, and OTPs stay in process memory and are not saved. The app
never asks for or stores the bKash PIN. PIN confirmation happens only in the
secure bKash browser window.

## Run

```powershell
.\.venv\Scripts\python.exe -m cinebot.ui.app
```

Then open <http://127.0.0.1:8765>.

The app dynamically checks the Cineplex guest API for the August 1 schedule,
finds the Spider-Man Hall 6 show inside the requested time window, fetches its
live seat map, verifies every E/F seat, and only then launches the four payment
sessions.

If the date, movie, or matching Hall 6 show has not been published yet, it stops
without creating a booking. If any E/F seat is unavailable, it also stops rather
than silently scattering the group or accepting random seats.

## Safety and reliability behavior

- Each transaction is capped at 10 seats.
- Each transaction stays within one physical row.
- The live runner verifies that every assigned seat appears in Cineplex's
  selected-seat summary before purchasing.
- Random-seat confirmation is never used.
- A failed session is never presented as successful.
- The final bKash PIN remains a manual action in the official payment window.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

