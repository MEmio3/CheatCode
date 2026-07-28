# CineBot — STAR Cineplex group booking console

A local Windows application for selecting a live STAR Cineplex show, choosing
specific seats, and coordinating up to eight separate bKash payment sessions.
It also includes an optional watcher that checks for a target show becoming
available and hands it to the normal booking flow.

The app uses a real, visible browser for the Cineplex and payment steps. It is
not a background payment service: you must be present to review each payment
and enter the final bKash PIN in the official bKash window.

## Requirements

- Windows 10 or 11
- Python 3.11 or later (`python --version`)
- Google Chrome is recommended (the app uses it when available)
- An internet connection
- A valid Cineplex/bKash account and the consent of every attendee whose name
  and bKash number you enter

## First-time setup

Open PowerShell in this project folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

`chromium` is the fallback browser used when Chrome cannot be launched. The
download is only needed once per virtual environment.

If PowerShell blocks activation, use the virtual environment's Python directly
in all commands below; no execution-policy change is required:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Start the app

After activating the environment, run either command:

```powershell
python -m cinebot.ui.app_v2
```

```powershell
.\start.bat
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). Keep the terminal
window open while using the app; press `Ctrl+C` to stop the server.

`start.bat` also opens the site automatically and clears a stale local server
that is already listening on port 8765. It runs the current picker interface.
The older `cinebot.ui.app` module is retained only as the fixed Hall 6 flow and
is available at `/legacy` from the current app.

## Book a currently available show

1. Open the local page and choose a location, date, movie, hall, showtime, and
   seat class. The app reads the live Cineplex catalogue and seat map.
2. Select the exact available seats. A payment can contain at most 10 seats;
   split larger groups across the required number of payment cards.
3. Enter one attendee name and bKash number for each payment, then start the
   group run. Check the seat labels and displayed totals before proceeding.
4. The app opens isolated, visible browser sessions and rechecks the show and
   selected seats before creating purchases. It pauses when each session asks
   for its bKash OTP.
5. Enter each OTP only in the matching prompt on the local page. Finish by
   entering the bKash PIN yourself in the matching official bKash window.
6. Wait for the page to show a confirmed payment for every session. Do not
   treat a browser window or OTP prompt as a successful ticket purchase.

If a seat becomes unavailable or a show changes before purchase, the run stops
with an error instead of substituting seats silently.

## Optional: watch for a show to become bookable

The **Watch for release** panel on the home page polls the live schedule and,
when the configured movie/hall/time becomes bookable, creates a live seat plan
and starts the normal group-run workflow.

1. Enter the movie, date, hall, time range, seat rule, polling interval, and
   attendee/payment details.
   Date is required; halls and the time range are optional preferences. Leave
   both time fields empty to accept any show time, and leave halls empty to
   accept any hall.
2. Click **Save target + payments**, review the values, then click **Start
   watching**.
3. Keep the local server running. On a planned restart, the watcher resumes
   because its local active marker is retained. Use **Stop sniper** to stop it
   intentionally and prevent a later resume.
4. When a match is found, review the live run, provide the OTPs, and manually
   complete the bKash PIN confirmations as above.

### Telegram watcher updates

In the Sniper mode panel, paste the Telegram bot token and your chat ID once
and click **Save Telegram**. The values are stored in the OS credential store,
not in `snipe_config.json`. While watching, CineBot sends one availability
status every 30 minutes and sends an immediate update when it finds a match or
encounters an error.

For a personal local setup, you can instead put these keys in a gitignored
`.env` file and restart CineBot:

```text
telegram_bot_token=123456:replace-with-your-token
userID=replace-with-your-chat-id
```

The release rule takes seats from the configured primary rows, omits the
configured trailing seats from each row, then fills any remainder from the
fill row. If rows are left empty, it selects available seats automatically from
the live layout. The application validates live availability and fails rather
than choosing replacements outside the selected rule.

## Privacy and payment safeguards

- bKash PINs and card CVVs are never requested, saved, or typed by CineBot.
- OTPs, names, and payment numbers entered for an active group run remain in
  process memory only.
- Watcher settings, including attendee names and bKash numbers, are saved to
  the local, gitignored `snipe_config.json`. Remove that file if you no longer
  want the saved watcher details on this computer.
- Optional credentials managed through `cinebot.config_store` use the OS
  keyring where available; its fallback file is `~/.cinebot/creds.json`.
- Each browser payment session uses a separate browser context. You should
  verify the attendee, seats, amount, and invoice displayed for that session
  before authorizing payment.

## Optional credential helper

The application does not need preconfigured credentials for the standard local
picker. If you use an optional supported integration, manage its values with:

```powershell
python -m cinebot.config_store set
python -m cinebot.config_store show
python -m cinebot.config_store backend
```

The helper refuses to store bKash PINs, CVVs, and passwords.

## Tests

Install the development dependencies during setup, then run:

```powershell
python -m pytest -q
```

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `python` is not recognized | Install Python 3.11+ from [python.org](https://www.python.org/downloads/) and enable **Add Python to PATH**, then open a new terminal. |
| Browser launch fails | Run `python -m playwright install chromium`; installing/updating Chrome is also recommended. |
| The page will not open | Confirm the terminal reports the server is running, then visit `http://127.0.0.1:8765`. Stop any stale instance with `Ctrl+C` or use `start.bat`. |
| Catalogue or seat-map request fails | Check your connection, retry shortly, and verify that the selected Cineplex date/show is published. Site-side changes, rate limits, or challenges can also prevent the live flow. |
| A selected seat is rejected | Refresh the live seat map and choose seats again. The app intentionally does not choose substitutes. |
| No OTP prompt appears | Check the matching visible bKash browser window for an error, cancellation, or changed payment page. Do not reuse an OTP from a different session. |

## Development notes

- `cinebot/ui/app_v2.py` serves the current picker and watcher UI.
- `cinebot/live/` contains the browser-backed Cineplex and bKash flow.
- `cinebot/sniper.py` contains the release watcher and live seat-plan rule.
- `docs/api-map.md` records the observed Cineplex API surface and recon notes.

Use this project only for legitimate ticket purchases and only where automated
interaction is permitted by the relevant service terms.
