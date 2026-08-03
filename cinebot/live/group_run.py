"""Focused live runner for the Hall 6 Spider-Man group booking.

The runner discovers the August 1 show through Cineplex's authenticated read
API, plans the complete E/F rows, and opens one real browser context per
contiguous <=10-seat transaction.  It pauses at bKash OTP and accepts the code
from the local control UI.  bKash PIN entry remains in the secure bKash window.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..browse import CineplexClient
from ..group import (
    TARGET_DATE,
    TARGET_HALL,
    TARGET_LOCATION,
    TARGET_LOCATION_ID,
    TARGET_MOVIE,
    TARGET_ROWS,
    GroupPlanError,
    SeatChunk,
    ShowChoice,
    choose_hall_show,
    display_show_time,
    mask_phone,
    movie_matches,
    plan_full_rows,
    validate_bkash_number,
    validate_names,
)
from .auth import ORIGIN, _UA

log = logging.getLogger("cinebot.live.group")


@dataclass
class PaymentSession:
    id: str
    index: int
    name: str
    phone_mask: str
    chunk: SeatChunk
    status: str = "queued"
    detail: str = "Waiting to launch"
    amount: Optional[int] = None
    invoice: Optional[str] = None
    otp_required: bool = False
    pin_required: bool = False
    error: Optional[str] = None
    _otp_future: Optional[asyncio.Future] = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "name": self.name,
            "phone": self.phone_mask,
            "row": self.chunk.row,
            "seats": list(self.chunk.labels),
            "seat_count": len(self.chunk.seats),
            "status": self.status,
            "detail": self.detail,
            "amount": self.amount,
            "invoice": self.invoice,
            "otp_required": self.otp_required,
            "pin_required": self.pin_required,
            "error": self.error,
        }


class GroupBookingManager:
    """One live group booking at a time, owned by the local FastAPI process."""

    def __init__(self) -> None:
        self.status = "idle"
        self.phase = "Ready"
        self.detail = "Enter the bKash number and four attendee names."
        self.error: Optional[str] = None
        self.show: Optional[dict[str, Any]] = None
        self.sessions: dict[str, PaymentSession] = {}
        self.started_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._browser = None
        self._contexts: list = []
        self.browser_open = False

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "phase": self.phase,
            "detail": self.detail,
            "error": self.error,
            "busy": self.busy,
            "show": self.show,
            "sessions": [
                session.public()
                for session in sorted(self.sessions.values(), key=lambda item: item.index)
            ],
            "started_at": self.started_at,
            "browser_open": self.browser_open,
        }

    async def start(self, bkash_number: str, names: list[str]) -> str:
        if self.busy:
            raise GroupPlanError("A group booking is already running.")
        bkash = validate_bkash_number(bkash_number)
        # E/F in the known Hall 6 layout produces four row-local chunks.
        cleaned_names = validate_names(names, required=4)
        self.status = "starting"
        self.phase = "Checking Cineplex"
        self.detail = "Looking for the August 1 Hall 6 Spider-Man show…"
        self.error = None
        self.show = None
        self.sessions.clear()
        self.started_at = time.time()
        run_id = f"group_{uuid.uuid4().hex[:10]}"
        self._task = asyncio.create_task(
            self._run(run_id, bkash, cleaned_names), name=run_id
        )
        return run_id

    async def stop(self) -> bool:
        if not self.busy:
            return False
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def close_browser(self) -> bool:
        """Manually close all browser windows."""
        closed = False
        for ctx in list(self._contexts):
            try:
                await ctx.close()
                closed = True
            except Exception:
                pass
        self._contexts.clear()
        if self._browser is not None:
            try:
                await self._browser.close()
                closed = True
            except Exception:
                pass
            self._browser = None
        self.browser_open = False
        return closed

    def submit_otp(self, session_id: str, code: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            raise GroupPlanError("Unknown payment session.")
        code = re.sub(r"\s+", "", code)
        if not re.fullmatch(r"\d{4,8}", code):
            raise GroupPlanError("Enter the numeric OTP from the matching bKash SMS.")
        future = session._otp_future
        if not session.otp_required or future is None or future.done():
            raise GroupPlanError("That payment session is not waiting for an OTP.")
        session.otp_required = False
        session.status = "submitting_otp"
        session.detail = "Submitting OTP securely to bKash…"
        future.set_result(code)

    async def _run(self, run_id: str, bkash: str, names: list[str]) -> None:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                try:
                    # Prefer the user's installed Chrome: it starts faster, is
                    # already present on this PC, and behaves like their normal
                    # Cineplex browser session without a separate download.
                    self._browser = await pw.chromium.launch(
                        channel="chrome", headless=False
                    )
                except Exception:
                    self._browser = await pw.chromium.launch(headless=False)
                self.browser_open = True
                show, chunks = await self._discover_show_and_seats(self._browser)
                names = validate_names(names, required=len(chunks))
                self.show = {
                    "movie": show.movie_title or TARGET_MOVIE,
                    "date": TARGET_DATE,
                    "hall": TARGET_HALL,
                    "time": display_show_time(show.show_time),
                    "program_id": show.program_id,
                    "rows": list(TARGET_ROWS),
                    "seat_count": sum(len(chunk.seats) for chunk in chunks),
                    "payments": len(chunks),
                    "unit_price": show.unit_price,
                }
                self.sessions = {}
                for chunk in chunks:
                    session_id = f"pay_{chunk.index}_{uuid.uuid4().hex[:6]}"
                    self.sessions[session_id] = PaymentSession(
                        id=session_id,
                        index=chunk.index,
                        name=names[chunk.index - 1],
                        phone_mask=mask_phone(bkash),
                        chunk=chunk,
                        amount=(
                            show.unit_price * len(chunk.seats)
                            if show.unit_price is not None
                            else None
                        ),
                    )
                self.status = "running"
                self.phase = "Preparing payments"
                self.detail = (
                    f"{self.show['seat_count']} seats secured in the plan; "
                    f"launching {len(chunks)} labeled payment sessions."
                )
                results = await asyncio.gather(
                    *(
                        self._run_payment_session(
                            self._browser, session, show, bkash
                        )
                        for session in self.sessions.values()
                    ),
                    return_exceptions=True,
                )
                for session, result in zip(self.sessions.values(), results):
                    if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        self._fail_session(session, str(result))
                failed = [session for session in self.sessions.values() if session.error]
                completed = [
                    session
                    for session in self.sessions.values()
                    if session.status == "completed"
                ]
                if failed:
                    self.status = "error"
                    self.phase = "Needs attention"
                    self.detail = (
                        f"{len(failed)} payment session(s) failed. "
                        "No failed session is treated as successful."
                    )
                    self.error = "; ".join(
                        f"{session.name}: {session.error}" for session in failed
                    )
                elif len(completed) == len(self.sessions):
                    self.status = "completed"
                    self.phase = "Booking complete"
                    self.detail = "All payment sessions returned successfully."
                else:
                    self.status = "attention"
                    self.phase = "Finish in bKash"
                    self.detail = "Complete the remaining secure PIN confirmations."
        except asyncio.CancelledError:
            self.status = "stopped"
            self.phase = "Stopped"
            self.detail = "The group booking run was stopped."
            for session in self.sessions.values():
                if session.status not in {"completed", "failed"}:
                    session.status = "stopped"
                    session.detail = "Stopped"
            raise
        except Exception as exc:
            log.exception("group run %s failed", run_id)
            self.status = "error"
            self.phase = "Could not start"
            self.detail = str(exc)
            self.error = str(exc)
        finally:
            # Browser stays open for manual payment completion.
            self._task = None

    async def _discover_show_and_seats(self, browser) -> tuple[ShowChoice, list[SeatChunk]]:
        self.phase = "Checking schedule"
        self.detail = "Signing in as a Cineplex guest to read the live schedule…"
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1360, "height": 900}
        )
        page = await context.new_page()
        try:
            token, device_key = await self._guest_login(page)
            client = CineplexClient(device_key=device_key, token=token)
            showdates = await asyncio.to_thread(
                client.get_showdates, TARGET_LOCATION_ID
            )
            target_day = next(
                (item for item in showdates if item.get("showDate") == TARGET_DATE),
                None,
            )
            if target_day is None:
                raise GroupPlanError(
                    "Cineplex has not published the August 1 ticket schedule yet. "
                    "Try again when August 1 appears on the ticket site."
                )
            movies = target_day.get("availableMovies") or []
            movie = next(
                (
                    item
                    for item in movies
                    if movie_matches(
                        str(
                            item.get("movie_title")
                            or item.get("movieTitle")
                            or item.get("title")
                            or ""
                        )
                    )
                ),
                None,
            )
            if movie is None:
                raise GroupPlanError(
                    "Spider-Man: Brand New Day is not bookable for August 1 yet."
                )
            movie_id = int(
                movie.get("movie_id") or movie.get("movieId") or movie.get("id") or 0
            )
            shows = await asyncio.to_thread(
                client.get_shows, TARGET_LOCATION_ID, movie_id, TARGET_DATE
            )
            show = choose_hall_show(shows)
            raw_seats = await asyncio.to_thread(
                client.get_seat_layout, TARGET_LOCATION_ID, show.program_id
            )
            seat_map = CineplexClient.raw_seats_to_seatmap(raw_seats)
            chunks = plan_full_rows(seat_map)
            return show, chunks
        finally:
            await context.close()

    async def _guest_login(self, page) -> tuple[str, str]:
        await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=30_000)
        button = page.get_by_role("button", name="Guest Login", exact=True)
        await button.wait_for(state="visible", timeout=15_000)
        async with page.expect_response(
            lambda response: "/api/v1/guest-login" in response.url,
            timeout=30_000,
        ) as response_info:
            await button.click()
        response = await response_info.value
        payload = await response.json()
        token = str((payload.get("data") or {}).get("token") or "")
        device_key = str(response.request.headers.get("device-key") or "")
        if not token or not device_key:
            raise GroupPlanError(
                "Cineplex guest login did not return a usable session. "
                "Complete any visible challenge and try again."
            )
        try:
            await page.wait_for_url(re.compile(r".*/home(?:\?.*)?$"), timeout=15_000)
        except Exception:
            pass
        return token, device_key

    async def _run_payment_session(
        self,
        browser,
        session: PaymentSession,
        show: ShowChoice,
        bkash: str,
    ) -> None:
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1320, "height": 900}
        )
        self._contexts.append(context)
        page = await context.new_page()
        try:
            self._set_session(session, "opening", "Opening a private Cineplex session…")
            await self._guest_login(page)

            self._set_session(session, "navigating", "Selecting the show…")
            await self._click_text(page, TARGET_LOCATION, exact=False)
            await self._click_text(page, re.compile(r"\b0?1\s+Aug\b", re.I))
            await self._click_text(
                page, re.compile(r"Spider[\s-]*Man:\s*Brand New Day", re.I)
            )
            show_card = page.locator("div.card-wrap.ticket-time").filter(
                has_text=re.compile(r"Hall\s*6", re.I)
            )
            show_link = show_card.locator("a").filter(
                has_text=re.compile(
                    re.escape(display_show_time(show.show_time)), re.I
                )
            ).first
            await show_link.wait_for(state="visible", timeout=15_000)
            await show_link.click()

            self._set_session(session, "selecting", "Selecting Premium and assigned seats…")
            seat_type = page.locator(".ticket_booking_left_item.seat_type").first
            await seat_type.wait_for(state="visible", timeout=15_000)
            await seat_type.click()
            premium = page.locator(".seat_type_select").get_by_text(
                re.compile(r"Premium", re.I)
            ).first
            await premium.wait_for(state="visible", timeout=10_000)
            await premium.click()

            plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
            await plus.wait_for(state="visible", timeout=10_000)
            for _ in session.chunk.seats:
                await plus.click()
                await page.wait_for_timeout(50)

            # JS-batch seat clicking for speed
            await self._dismiss_swal2(page)
            labels = list(session.chunk.labels)
            log.info(f"[Session {session.index}] Attempting JS batch click for seats: {labels}")
            clicked_count = await self._batch_click_seats(page, labels)
            log.info(f"[Session {session.index}] JS batch clicked {clicked_count}/{len(labels)} seats.")

            await page.wait_for_timeout(500)
            selected_text = await page.locator(".selected_seat").inner_text(timeout=5_000)
            accepted_labels = set(re.findall(r"\b[A-Z]{1,2}\d+\b", selected_text.upper()))
            missing = [label for label in labels if label.upper() not in accepted_labels]

            if missing:
                log.warning(f"[Session {session.index}] JS batch missed seats: {missing}. Falling back to sequential clicks for missing seats.")
                for label in missing:
                    await self._dismiss_swal2(page)
                    seat = page.get_by_text(label, exact=True).first
                    await seat.click(timeout=5_000)
                    log.info(f"[Session {session.index}] Clicked {label} sequentially.")
                    await page.wait_for_timeout(200)

            selected_text = await page.locator(".selected_seat").inner_text(timeout=5_000)
            accepted_labels = set(re.findall(r"\b[A-Z]{1,2}\d+\b", selected_text.upper()))
            missing = [label for label in labels if label.upper() not in accepted_labels]

            if missing:
                err_msg = "Cineplex did not accept assigned seats: " + ", ".join(missing)
                log.error(f"[Session {session.index}] {err_msg}")
                raise GroupPlanError(err_msg)
            
            log.info(f"[Session {session.index}] Seats successfully selected: {accepted_labels}")

            log.info(f"[Session {session.index}] Filling attendee details...")
            await page.fill("input[name=customer_name]", session.name)
            await page.fill("input[name=msisdn]", bkash)
            await page.fill("input[name=msisdn_confirm]", bkash)
            terms = page.locator("input[name=terms]").first
            if not await terms.is_checked():
                await terms.check(force=True)

            try:
                total_text = await page.locator(".total_amount").inner_text(
                    timeout=2_000
                )
                amount_match = re.search(r"[\d,]+", total_text)
                if amount_match:
                    session.amount = int(amount_match.group(0).replace(",", ""))
            except Exception:
                pass

            purchase = page.locator("button.btn-desktop-purchase").first
            await self._wait_enabled(purchase, timeout_ms=30_000)
            log.info(f"[Session {session.index}] Waiting for group release gate...")
            await self._wait_for_group_release(session)

            # Seat clicks never reserve anything server-side; only this Purchase
            # POST does. Every session books a distinct row, so there is no
            # seat conflict between our own sessions to stagger around — any
            # delay here just exposes the seats to external snipers. Fire the
            # instant the gate releases so all sessions hit /booking together.
            log.info(f"[Session {session.index}] Gate released. Clicking Purchase...")
            self._set_session(session, "booking", "Creating the Cineplex booking…")
            async with page.expect_response(
                lambda response: re.search(r"/api/v1/booking$", response.url)
                is not None,
                timeout=30_000,
            ) as booking_response_info:
                await purchase.click()
            booking_response = await booking_response_info.value
            booking_payload = await booking_response.json()
            if booking_payload.get("code") != 200:
                messages = booking_payload.get("message") or ["Booking rejected"]
                log.error(f"[Session {session.index}] Booking rejected: {messages[0]}")
                raise GroupPlanError(str(messages[0]))

            log.info(f"[Session {session.index}] Booking created. Proceeding to payment gateway...")
            self._set_session(session, "gateway", "Opening secure bKash payment…")
            mobile = page.get_by_text(
                re.compile(r"MOBILE BANKING", re.I), exact=False
            ).first
            await mobile.wait_for(state="visible", timeout=45_000)
            await mobile.click()
            bkash_option = page.locator(
                "img[src*='bkash' i], [alt*='bkash' i], [class*='bkash' i], "
                "[style*='bkash' i]"
            ).first
            try:
                await bkash_option.wait_for(state="visible", timeout=12_000)
                await bkash_option.click()
            except Exception:
                await page.get_by_text(re.compile(r"\bbKash\b", re.I)).first.click(
                    timeout=5_000
                )

            log.info(f"[Session {session.index}] Filling wallet number: {bkash}")
            wallet = page.locator(
                'input[name="WALLET"], input[name*="WALLET"], '
                'input[placeholder*="01X"], input[type="tel"]'
            ).first
            await wallet.wait_for(state="visible", timeout=20_000)
            await wallet.fill(bkash)
            body_text = await page.locator("body").inner_text()
            invoice_match = re.search(r"Inv\s*No:\s*([A-Z0-9-]+)", body_text, re.I)
            if invoice_match:
                session.invoice = invoice_match.group(1)
            confirm = page.locator(
                "button:has-text('Confirm'), [role=button]:has-text('Confirm'), "
                "a:has-text('Confirm')"
            ).last
            log.info(f"[Session {session.index}] Clicking bKash Confirm button...")
            await confirm.click(timeout=8_000)

            # Manual payment: stop automation, let user handle OTP + PIN.
            log.info(f"[Session {session.index}] Stopping automation for manual OTP/PIN entry.")
            self._set_session(
                session,
                "manual_payment",
                "Complete OTP and PIN manually in the bKash browser window.",
            )
            await self._wait_for_payment_result(page, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_session(session, str(exc))
        # Browser context stays open for manual payment.

    async def _batch_click_seats(self, page, labels: list[str]) -> int:
        """Click all target seats in one JS evaluate call for speed."""
        try:
            result = await page.evaluate(
                """(labels) => {
                    let clicked = 0;
                    for (const label of labels) {
                        let el = document.querySelector(`[data-seat="${label}"]`);
                        if (!el) {
                            const all = document.querySelectorAll('a, button');
                            for (const node of all) {
                                if (node.textContent.trim() === label) { el = node; break; }
                            }
                        }
                        if (el) {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            clicked++;
                        }
                    }
                    return clicked;
                }""",
                labels,
            )
            await page.wait_for_timeout(300)
            return int(result) if str(result).isdigit() else (len(labels) if result is True else 0)
        except Exception as exc:
            log.warning("JS batch seat click failed, using fallback: %s", exc)
            return 0

    async def _dismiss_swal2(self, page) -> None:
        """Dismiss SweetAlert2 popups that intercept pointer events."""
        try:
            if await page.evaluate("document.querySelector('.swal2-confirm') !== null"):
                btn = page.locator(".swal2-confirm").first
                if await btn.is_visible(timeout=200):
                    await btn.click(timeout=1500)
        except Exception:
            pass

    async def _wait_for_payment_result(
        self, page, session: PaymentSession, timeout_seconds: int = 600
    ) -> None:
        _FAIL_PATTERNS = (
            "payment failed", "transaction failed", "insufficient balance",
            "insufficient fund", "transaction limit", "session expired",
            "payment cancelled", "payment canceled", "could not process",
            "try again later", "something went wrong",
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if page.is_closed():
                raise GroupPlanError(
                    "The bKash window closed before payment confirmation."
                )
            url = page.url.casefold()
            try:
                text = (await page.locator("body").inner_text(timeout=1_500)).casefold()
            except Exception:
                text = ""
            if (
                "ticket purchase success" in text
                or "payment successful" in text
                or "transaction successful" in text
                or ("cineplex" in url and "success" in url)
            ):
                session.pin_required = False
                self._set_session(session, "completed", "Payment confirmed.")
                return
            for pattern in _FAIL_PATTERNS:
                if pattern in text:
                    raise GroupPlanError(f"bKash payment failed: {pattern}")
            if "fail" in url or "cancel" in url or "error" in url:
                raise GroupPlanError("Payment gateway returned a failure redirect.")
            await page.wait_for_timeout(1_500)
        # TODO: group_booking.py silently returns on timeout (letting the user
        # finish manual OTP/PIN), but this file raises. Align the two once the
        # preferred behavior is decided.
        raise GroupPlanError(
            "Timed out waiting for the secure bKash PIN/payment confirmation."
        )

    async def _click_text(self, page, value, *, exact: bool = False) -> None:
        locator = page.get_by_text(value, exact=exact).first
        await locator.wait_for(state="visible", timeout=15_000)
        await locator.click()

    async def _wait_enabled(self, locator, *, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1_000
        await locator.wait_for(state="visible", timeout=timeout_ms)
        while time.monotonic() < deadline:
            if await locator.is_enabled():
                return
            await asyncio.sleep(0.1)
        raise GroupPlanError("Cineplex did not enable the Purchase button in time.")

    def _set_session(
        self, session: PaymentSession, status: str, detail: str
    ) -> None:
        session.status = status
        session.detail = detail
        if status not in {"waiting_otp", "submitting_otp"}:
            session.otp_required = False

    def _fail_session(self, session: PaymentSession, detail: str) -> None:
        session.status = "failed"
        session.detail = detail
        session.error = detail
        session.otp_required = False
        session.pin_required = False
