"""Four-session runner for an exact show and seats chosen in the live picker."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from ..browse import CineplexClient
from ..group import (
    GroupPlanError,
    SeatChunk,
    display_show_time,
    mask_phone,
    parse_show_time,
    validate_bkash_number,
    validate_names,
)
from .auth import ORIGIN, _UA
from .catalog import available_seats_by_label

log = logging.getLogger("cinebot.live.group")


@dataclass(frozen=True)
class BookingTarget:
    location_id: int
    location_name: str
    show_date: str
    movie_id: int
    movie_title: str
    program_id: int
    screen_id: int
    hall_name: str
    show_time: str
    seat_type_id: int
    seat_type_name: str
    unit_price: int


@dataclass(frozen=True)
class PaymentRequest:
    index: int
    name: str
    bkash: str
    seat_labels: tuple[str, ...]


@dataclass
class PaymentSession:
    id: str
    index: int
    name: str
    phone: str
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
            "phone": self.phone,
            "phone_mask": mask_phone(self.phone),
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


def target_from_payload(payload: dict[str, Any]) -> BookingTarget:
    try:
        target = BookingTarget(
            location_id=int(payload["location_id"]),
            location_name=str(payload["location_name"]).strip(),
            show_date=str(payload["show_date"]).strip(),
            movie_id=int(payload["movie_id"]),
            movie_title=str(payload["movie_title"]).strip(),
            program_id=int(payload["program_id"]),
            screen_id=int(payload["screen_id"]),
            hall_name=str(payload["hall_name"]).strip(),
            show_time=str(payload["show_time"]).strip(),
            seat_type_id=int(payload["seat_type_id"]),
            seat_type_name=str(payload["seat_type_name"]).strip(),
            unit_price=int(payload.get("unit_price") or 0),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GroupPlanError("The selected show information is incomplete.") from exc
    if min(
        target.location_id,
        target.movie_id,
        target.program_id,
        target.screen_id,
        target.seat_type_id,
    ) <= 0:
        raise GroupPlanError("The selected show contains an invalid identifier.")
    if not all(
        (
            target.location_name,
            target.movie_title,
            target.hall_name,
            target.seat_type_name,
        )
    ):
        raise GroupPlanError("The selected show information is incomplete.")
    try:
        datetime.strptime(target.show_date, "%Y-%m-%d")
        parse_show_time(target.show_time)
    except (ValueError, GroupPlanError) as exc:
        raise GroupPlanError("The selected date or show time is invalid.") from exc
    return target


def payments_from_payload(
    payloads: list[dict[str, Any]], *, allow_duplicate_identity: bool = False
) -> list[PaymentRequest]:
    if not 1 <= len(payloads) <= 8:
        raise GroupPlanError("Between 1 and 8 payment sessions are required.")
    names = validate_names(
        [str(item.get("name") or "") for item in payloads],
        len(payloads),
        allow_duplicates=allow_duplicate_identity,
    )
    phones = [
        validate_bkash_number(str(item.get("bkash_number") or ""))
        for item in payloads
    ]
    if not allow_duplicate_identity and len(set(phones)) != len(phones):
        raise GroupPlanError("Use a different bKash number for each session.")

    requests: list[PaymentRequest] = []
    all_labels: list[str] = []
    for index, (payload, name, phone) in enumerate(zip(payloads, names, phones), 1):
        labels = tuple(
            re.sub(r"\s+", "", str(label)).upper()
            for label in payload.get("seats") or []
            if str(label).strip()
        )
        if not 1 <= len(labels) <= 10:
            raise GroupPlanError(
                f"Payment {index} needs between 1 and 10 selected seats."
            )
        if len(set(labels)) != len(labels):
            raise GroupPlanError(f"Payment {index} contains a duplicate seat.")
        if any(not re.fullmatch(r"[A-Z]{1,3}\d+", label) for label in labels):
            raise GroupPlanError(f"Payment {index} contains an invalid seat label.")
        all_labels.extend(labels)
        requests.append(PaymentRequest(index, name, phone, labels))
    if len(set(all_labels)) != len(all_labels):
        raise GroupPlanError("The same seat cannot belong to two payments.")
    return requests


class GroupBookingManager:
    """Own one configurable multi-payment booking run for the local control page."""

    def __init__(self) -> None:
        self.status = "idle"
        self.phase = "Ready"
        self.detail = "Load a show, choose seats, then enter payment details."
        self.error: Optional[str] = None
        self.show: Optional[dict[str, Any]] = None
        self.sessions: dict[str, PaymentSession] = {}
        self.started_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._browser = None
        self._contexts: list = []
        self._ready_event: asyncio.Event | None = None
        self._ready_lock: asyncio.Lock | None = None
        self._ready_count = 0
        self._ready_expected = 0
        self._gate_error: Optional[str] = None
        self._purchases_released = False
        self._park_event: Optional[asyncio.Event] = None
        # Serializes the /booking POST across sessions: Cineplex rejects
        # overlapping booking calls from the same IP with a bogus "already
        # booked" message, so exactly one may be in flight at a time.
        self._booking_lock: asyncio.Lock | None = None
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
                item.public()
                for item in sorted(self.sessions.values(), key=lambda value: value.index)
            ],
            "started_at": self.started_at,
            "browser_open": self.browser_open,
        }

    async def start(
        self,
        target_payload: dict[str, Any],
        payment_payloads: list[dict[str, Any]],
        *,
        allow_duplicate_identity: bool = False,
    ) -> str:
        if self.busy:
            raise GroupPlanError("A group booking is already running.")
        target = target_from_payload(target_payload)
        payments = payments_from_payload(
            payment_payloads,
            allow_duplicate_identity=allow_duplicate_identity,
        )
        self.status = "starting"
        self.phase = "Rechecking selection"
        self.detail = "Confirming the show and selected seats against live data."
        self.error = None
        self.show = None
        self.sessions.clear()
        self.started_at = time.time()
        self._ready_event = asyncio.Event()
        self._ready_lock = asyncio.Lock()
        self._ready_count = 0
        self._ready_expected = len(payments)
        self._gate_error = None
        self._purchases_released = False
        self._park_event = asyncio.Event()
        self._booking_lock = asyncio.Lock()
        run_id = f"group_{uuid.uuid4().hex[:10]}"
        self._task = asyncio.create_task(self._run(run_id, target, payments), name=run_id)
        return run_id

    async def stop(self) -> bool:
        if not self.busy:
            return False
        assert self._task is not None
        if self._park_event is not None:
            self._park_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def close_browser(self) -> bool:
        """Manually close all browser windows. Called from the UI."""
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
        session.detail = "Submitting OTP securely to bKash..."
        future.set_result(code)

    async def _run(
        self, run_id: str, target: BookingTarget, payments: list[PaymentRequest]
    ) -> None:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                try:
                    self._browser = await pw.chromium.launch(channel="chrome", headless=False)
                except Exception:
                    self._browser = await pw.chromium.launch(headless=False)
                self.browser_open = True
                target, chunks = await self._verify_target_and_seats(
                    self._browser, target, payments
                )
                self.show = {
                    "movie": target.movie_title,
                    "date": target.show_date,
                    "location": target.location_name,
                    "hall": target.hall_name,
                    "time": display_show_time(target.show_time),
                    "program_id": target.program_id,
                    "seat_type": target.seat_type_name,
                    "seat_count": sum(len(chunk.seats) for chunk in chunks),
                    "payments": len(chunks),
                    "unit_price": target.unit_price,
                }
                for request, chunk in zip(payments, chunks):
                    session_id = f"pay_{request.index}_{uuid.uuid4().hex[:6]}"
                    self.sessions[session_id] = PaymentSession(
                        id=session_id,
                        index=request.index,
                        name=request.name,
                        phone=request.bkash,
                        chunk=chunk,
                        amount=target.unit_price * len(chunk.seats),
                    )
                self.status = "running"
                self.phase = f"Preparing {len(payments)} session{'s' if len(payments) != 1 else ''}"
                self.detail = f"Opening {len(payments)} isolated session{'s' if len(payments) != 1 else ''} and verifying exact seats."
                results = await asyncio.gather(
                    *(
                        self._run_payment_session(self._browser, session, target)
                        for session in self.sessions.values()
                    ),
                    return_exceptions=True,
                )
                for session, result in zip(self.sessions.values(), results):
                    if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        self._fail_session(session, str(result))
                failed = [item for item in self.sessions.values() if item.error]
                completed = [
                    item for item in self.sessions.values() if item.status == "completed"
                ]
                if failed:
                    self.status = "error"
                    self.phase = "Needs attention"
                    self.detail = f"{len(failed)} payment session(s) failed."
                    self.error = "; ".join(
                        f"{item.name}: {item.error}" for item in failed
                    )
                elif len(completed) == len(self.sessions):
                    self.status = "completed"
                    self.phase = "Booking complete"
                    self.detail = "All payment sessions returned successfully."
                else:
                    self.status = "attention"
                    self.phase = "Finish in bKash"
                    self.detail = "Complete the remaining secure PIN confirmations."
                # Park inside the playwright context so browser windows stay
                # open for manual OTP + PIN. Exits on Stop.
                if not hasattr(self, '_park_event') or self._park_event is None:
                    self._park_event = asyncio.Event()
                try:
                    await self._park_event.wait()
                except asyncio.CancelledError:
                    raise
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
            # Browser windows stay open so the user can complete manual payments.
            # Call close_browser() from the UI when done.
            self._task = None

    async def _verify_target_and_seats(
        self,
        browser,
        target: BookingTarget,
        payments: list[PaymentRequest],
    ) -> tuple[BookingTarget, list[SeatChunk]]:
        self.phase = "Live preflight"
        self.detail = "Rechecking the published show and selected seats..."
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1360, "height": 900}
        )
        page = await context.new_page()
        try:
            token, device_key = await self._guest_login(page)
            client = CineplexClient(device_key=device_key, token=token)
            showdates = await asyncio.to_thread(client.get_showdates, target.location_id)
            target_day = next(
                (item for item in showdates if item.get("showDate") == target.show_date),
                None,
            )
            if target_day is None:
                raise GroupPlanError("The selected date is no longer published.")
            movie = next(
                (
                    item
                    for item in target_day.get("availableMovies") or []
                    if int(item.get("movie_id") or item.get("movieId") or 0)
                    == target.movie_id
                ),
                None,
            )
            if movie is None:
                raise GroupPlanError("The selected movie is no longer listed for that date.")

            shows = await asyncio.to_thread(
                client.get_shows,
                target.location_id,
                target.movie_id,
                target.show_date,
            )
            canonical = None
            for screen in shows:
                screen_id = int(screen.get("screenID") or screen.get("screenId") or 0)
                for slot in screen.get("showTimes") or []:
                    if int(slot.get("programId") or 0) == target.program_id:
                        canonical = (screen, slot, screen_id)
                        break
                if canonical:
                    break
            if canonical is None:
                raise GroupPlanError("The selected hall/show time is no longer published.")
            screen, slot, screen_id = canonical
            if screen_id != target.screen_id:
                raise GroupPlanError("The selected show moved halls. Reload the schedule.")

            price = next(
                (
                    item
                    for item in slot.get("seatPrices") or []
                    if int(
                        item.get("seatTypeId")
                        or item.get("seatTypeID")
                        or item.get("classId")
                        or 0
                    )
                    == target.seat_type_id
                ),
                None,
            )
            if price is None:
                raise GroupPlanError("The selected seat class is no longer available.")
            target = replace(
                target,
                movie_title=str(
                    movie.get("movie_title")
                    or movie.get("movieTitle")
                    or target.movie_title
                ),
                hall_name=str(
                    screen.get("screenTitle")
                    or screen.get("screenName")
                    or target.hall_name
                ),
                show_time=str(slot.get("showTime") or target.show_time),
                seat_type_name=str(
                    price.get("seatTypeName")
                    or price.get("seatTypeTitle")
                    or target.seat_type_name
                ),
                unit_price=int(price.get("unitPrice") or target.unit_price or 0),
            )

            raw_seats = await asyncio.to_thread(
                client.get_seat_layout, target.location_id, target.program_id
            )
            available = available_seats_by_label(raw_seats, target.seat_type_id)
            seat_map = CineplexClient.raw_seats_to_seatmap(raw_seats)
            by_label = {
                f"{seat.row_label}{seat.col_label}".upper(): seat
                for seat in seat_map.seats
            }
            chunks: list[SeatChunk] = []
            for request in payments:
                missing = [label for label in request.seat_labels if label not in available]
                if missing:
                    raise GroupPlanError(
                        "These selected seats are no longer available: "
                        + ", ".join(missing)
                    )
                seats = tuple(by_label[label] for label in request.seat_labels)
                rows = list(dict.fromkeys(seat.row_label for seat in seats))
                chunks.append(
                    SeatChunk(request.index, " / ".join(rows), seats)
                )
            return target, chunks
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
            raise GroupPlanError("Cineplex guest login did not return a usable session.")
        try:
            await page.wait_for_url(re.compile(r".*/home(?:\?.*)?$"), timeout=15_000)
        except Exception:
            pass
        return token, device_key

    async def _run_payment_session(
        self, browser, session: PaymentSession, target: BookingTarget
    ) -> None:
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1320, "height": 900}
        )
        self._contexts.append(context)
        page = await context.new_page()
        try:
            await self._show_window_label(page, session, "Opening")
            stagger = max(0, session.index - 1) * 2
            if stagger:
                self._set_session(
                    session,
                    "queued",
                    f"Launch position {session.index}; opening in {stagger:.0f}s.",
                )
                await asyncio.sleep(stagger)
            self._set_session(session, "opening", "Opening a private session...")
            await self._guest_login(page)
            await self._show_window_label(page, session, "Opening")
            self._set_session(session, "navigating", "Opening the selected show...")
            await self._click_text(page, target.location_name, exact=False)
            await self._click_text(page, self._date_pattern(target.show_date))
            await self._click_text(page, target.movie_title, exact=False)

            show_card = page.locator("div.card-wrap.ticket-time").filter(
                has_text=re.compile(re.escape(target.hall_name), re.I)
            )
            show_link = show_card.locator("a").filter(
                has_text=re.compile(re.escape(display_show_time(target.show_time)), re.I)
            ).first
            await show_link.wait_for(state="visible", timeout=15_000)
            await show_link.click()

            self._set_session(
                session, "selecting", f"Selecting {target.seat_type_name} seats..."
            )
            seat_type = page.locator(".ticket_booking_left_item.seat_type").first
            await seat_type.wait_for(state="visible", timeout=15_000)
            await seat_type.click()
            type_choice = page.locator(".seat_type_select").get_by_text(
                re.compile(re.escape(target.seat_type_name), re.I)
            ).first
            await type_choice.wait_for(state="visible", timeout=10_000)
            await type_choice.click()

            log.info(f"[Session {session.index}] Setting quantity to {len(session.chunk.seats)}...")
            plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
            await plus.wait_for(state="visible", timeout=10_000)
            for _ in session.chunk.seats:
                await plus.click()
                await page.wait_for_timeout(50)
            # The seat map renders asynchronously after the quantity is set.
            # Clicking into a half-rendered layout selects seats whose booking
            # data isn't bound yet, which is rejected as "Invalid data". So wait
            # until every target seat is actually present and visible first.
            labels = list(session.chunk.labels)
            await self._dismiss_swal2(page)
            log.info(f"[Session {session.index}] Waiting for seat map to render {labels}...")
            for label in labels:
                await self._wait_for_seat(page, label)
            await page.wait_for_timeout(250)  # settle so the seat data model binds

            # JS-batch seat clicking: click all seats at once via DOM for speed.
            log.info(f"[Session {session.index}] Attempting JS batch click for seats: {labels}")
            clicked_count = await self._batch_click_seats(page, labels)
            log.info(f"[Session {session.index}] JS batch clicked {clicked_count}/{len(labels)} seats.")
            
            # Verify and fallback to click ONLY missing seats
            await page.wait_for_timeout(500)
            selected_text = await page.locator(".selected_seat").inner_text(timeout=5_000)
            accepted = set(re.findall(r"\b[A-Z]{1,3}\d+\b", selected_text.upper()))
            missing = [label for label in labels if label not in accepted]
            
            if missing:
                log.warning(f"[Session {session.index}] JS batch missed seats: {missing}. Falling back to sequential clicks for missing seats.")
                for label in missing:
                    await self._dismiss_swal2(page)
                    seat = await self._wait_for_seat(page, label)
                    await seat.click(timeout=5_000)
                    log.info(f"[Session {session.index}] Clicked {label} sequentially.")
                    await page.wait_for_timeout(50)

            # Final verification
            selected_text = await page.locator(".selected_seat").inner_text(timeout=5_000)
            accepted = set(re.findall(r"\b[A-Z]{1,3}\d+\b", selected_text.upper()))
            missing = [label for label in labels if label not in accepted]
            if missing:
                err_msg = "Cineplex did not accept assigned seats: " + ", ".join(missing)
                log.error(f"[Session {session.index}] {err_msg}")
                raise GroupPlanError(err_msg)
            
            log.info(f"[Session {session.index}] Seats successfully selected: {accepted}")

            log.info(f"[Session {session.index}] Filling attendee details...")
            await page.fill("input[name=customer_name]", session.name)
            await page.fill("input[name=msisdn]", session.phone)
            await page.fill("input[name=msisdn_confirm]", session.phone)
            terms = page.locator("input[name=terms]").first
            if not await terms.is_checked():
                await terms.check(force=True)
            try:
                total_text = await page.locator(".total_amount").inner_text(timeout=2_000)
                amount_match = re.search(r"[\d,]+", total_text)
                if amount_match:
                    session.amount = int(amount_match.group(0).replace(",", ""))
            except Exception:
                pass

            purchase = page.locator("button.btn-desktop-purchase").first
            await self._wait_enabled(purchase, timeout_ms=30_000)
            log.info(f"[Session {session.index}] Waiting for group release gate...")
            await self._wait_for_group_release(session)

            # The Purchase click is what actually holds+books the seats on the
            # server (seat clicks never touch it). Cineplex's guest backend
            # rejects two /booking POSTs that overlap in time from the same IP
            # with a bogus "already booked by another user" message — even in an
            # empty hall. So serialize: exactly one booking POST is in flight at
            # a time. Nobody is sniping, so waiting our turn is free.
            self._set_session(session, "booking", "Waiting for booking slot...")
            log.info(f"[Session {session.index}] Gate released; queuing for serial booking...")
            assert self._booking_lock is not None
            max_attempts = 3
            booking_payload: dict = {}
            async with self._booking_lock:
                for attempt in range(1, max_attempts + 1):
                    self._set_session(session, "booking", "Creating the Cineplex booking...")
                    log.info(
                        f"[Session {session.index}] Booking slot acquired. "
                        f"Clicking Purchase (attempt {attempt}/{max_attempts})..."
                    )
                    async with page.expect_response(
                        lambda response: re.search(r"/api/v1/booking$", response.url) is not None,
                        timeout=30_000,
                    ) as booking_response_info:
                        await purchase.click()
                    booking_response = await booking_response_info.value
                    try:
                        booking_payload = await booking_response.json()
                    except Exception:
                        booking_payload = {}
                    try:
                        post_data = booking_response.request.post_data
                    except Exception:
                        post_data = None
                    log.info(
                        f"[Session {session.index}] /booking -> HTTP {booking_response.status}; "
                        f"code={booking_payload.get('code')}; sent={post_data}"
                    )
                    if booking_payload.get("code") == 200:
                        break
                    # Rejected (e.g. the "seat already booked" modal). Click the
                    # modal's OK button to dismiss it, then click Purchase again
                    # with the same seats. In an empty hall this rejection is
                    # usually transient and clears on retry.
                    messages = booking_payload.get("message") or ["Booking rejected"]
                    log.warning(
                        f"[Session {session.index}] Booking rejected on attempt {attempt}: "
                        f"{messages[0]}"
                    )
                    if attempt < max_attempts:
                        await self._dismiss_swal2(page)
                        await page.wait_for_timeout(800)
                        try:
                            await self._wait_enabled(purchase, timeout_ms=10_000)
                        except Exception:
                            pass
                # Let the backend fully commit this hold before the next
                # session's booking POST begins.
                await page.wait_for_timeout(600)
            if booking_payload.get("code") != 200:
                messages = booking_payload.get("message") or ["Booking rejected"]
                log.error(
                    f"[Session {session.index}] Booking rejected after {max_attempts} attempts: "
                    f"{messages[0]}"
                )
                raise GroupPlanError(str(messages[0]))

            log.info(f"[Session {session.index}] Booking created. Proceeding to payment gateway...")
            self._set_session(session, "gateway", "Opening secure bKash payment...")
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

            log.info(f"[Session {session.index}] Filling wallet number: {session.phone}")
            wallet = page.locator(
                'input[name="WALLET"], input[name*="WALLET"], '
                'input[placeholder*="01X"], input[type="tel"]'
            ).first
            await wallet.wait_for(state="visible", timeout=20_000)
            await wallet.fill(session.phone)
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

            otp_input = page.locator(
                "input[placeholder*='6 digit' i], input[placeholder*='code' i], "
                "input[name*='otp' i], input[id*='otp' i]"
            ).first
            await otp_input.wait_for(state="visible", timeout=25_000)
            # MANUAL HANDOFF: the bot stops here. The user enters OTP + PIN
            # directly in this browser window. We do not automate, do not close.
            session.otp_required = False
            session.pin_required = True
            self._set_session(
                session,
                "manual_otp",
                "Reached the bKash OTP page. Enter OTP + PIN in this window.",
            )
            await self._show_window_label(page, session, "Enter OTP + PIN here")
            # Watch the window for success/failure without entering anything.
            await self._wait_for_payment_result(page, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._purchases_released:
                await self._release_after_failure()
            self._fail_session(session, str(exc))
        finally:
            if session._otp_future is not None and not session._otp_future.done():
                session._otp_future.cancel()
            # Intentionally do NOT close the context — leave the window open
            # for manual OTP + PIN.

    async def _wait_for_group_release(self, session: PaymentSession) -> None:
        assert self._ready_event is not None
        assert self._ready_lock is not None
        async with self._ready_lock:
            self._ready_count += 1
            self._set_session(
                session,
                "ready",
                f"Seats verified. Ready {self._ready_count} of {self._ready_expected}.",
            )
            if self._ready_count >= self._ready_expected:
                self._purchases_released = True
                self.phase = "All ready"
                self.detail = f"All {self._ready_expected} seat set(s) verified. Releasing purchases together."
                self._ready_event.set()
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=15)
        except TimeoutError:
            # Release (proceed) instead of aborting — non-blocking gate.
            self._ready_event.set()
        if self._gate_error:
            raise GroupPlanError(
                "Purchases stopped before booking because another session failed: "
                + self._gate_error
            )

    async def _release_after_failure(self) -> None:
        """Decrement expected ready count and release the gate if the
        remaining ready sessions still meet quorum."""
        if self._ready_event is None:
            return
        assert self._ready_lock is not None
        async with self._ready_lock:
            if self._ready_expected > 0:
                self._ready_expected -= 1
            # Quorum: proceed as long as the ready sessions still meet the
            # (now-reduced) expected count, or none remain expected.
            if self._ready_count >= self._ready_expected:
                self._purchases_released = True
                self._ready_event.set()

    async def _batch_click_seats(self, page, labels: list[str]) -> int:
        """Click every target seat in one JS evaluate call for speed.

        The live Cineplex seat cells are NOT <a>/<button>; they are generic
        elements whose exact trimmed text is the seat label (e.g. "D5"). So
        match by exact text across every element, pick the tightest match
        (fewest descendants = the seat cell, not a container), and fire a full
        mousedown/mouseup/click sequence so the SPA's real handler runs.
        """
        try:
            clicked = await page.evaluate(
                """(labels) => {
                    const want = new Set(labels);
                    const best = {};
                    for (const el of document.querySelectorAll('*')) {
                        const txt = (el.textContent || '').trim();
                        if (!want.has(txt)) continue;
                        const depth = el.querySelectorAll('*').length;
                        if (best[txt] === undefined || depth < best[txt].depth) {
                            best[txt] = { el, depth };
                        }
                    }
                    const done = [];
                    for (const label of labels) {
                        const hit = best[label];
                        if (!hit) continue;
                        const el = hit.el;
                        try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
                        const opts = { bubbles: true, cancelable: true, view: window };
                        el.dispatchEvent(new MouseEvent('mousedown', opts));
                        el.dispatchEvent(new MouseEvent('mouseup', opts));
                        el.dispatchEvent(new MouseEvent('click', opts));
                        done.push(label);
                    }
                    return done;
                }""",
                labels,
            )
            await page.wait_for_timeout(250)
            return len(clicked) if isinstance(clicked, list) else 0
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
            "payment failed",
            "transaction failed",
            "insufficient balance",
            "insufficient fund",
            "transaction limit",
            "session expired",
            "payment cancelled",
            "payment canceled",
            "could not process",
            "try again later",
            "something went wrong",
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if page.is_closed():
                raise GroupPlanError("The bKash window closed before confirmation.")
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
# Immediate failure detection
            for pattern in _FAIL_PATTERNS:
                if pattern in text:
                    raise GroupPlanError(f"bKash payment failed: {pattern}")
            if "fail" in url or "cancel" in url or "error" in url:
                raise GroupPlanError("Payment gateway returned a failure redirect.")
            await page.wait_for_timeout(1_500)
        # On timeout, do NOT raise — leave the session in manual_otp so the
        # user can still finish the OTP + PIN entry by hand.
        return

    def _date_pattern(self, value: str) -> re.Pattern[str]:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return re.compile(
            rf"\b(?:0?{parsed.day}\s+{parsed.strftime('%b')}|"
            rf"{parsed.strftime('%b')}\s+0?{parsed.day})\b",
            re.I,
        )

    async def _click_text(self, page, value, *, exact: bool = False) -> None:
        locator = page.get_by_text(value, exact=exact).first
        await locator.wait_for(state="visible", timeout=15_000)
        await locator.click()

    async def _show_window_label(self, page, session: PaymentSession, state: str) -> None:
        """Put the payment order directly on the real payment browser window."""
        try:
            await page.evaluate(
                """({index, state}) => {
                    let el = document.getElementById('cinebot-payment-order');
                    if (!el) {
                        el = document.createElement('div');
                        el.id = 'cinebot-payment-order';
                        el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 16px;background:#24112f;color:#fff;font:700 16px system-ui;text-align:center;box-shadow:0 2px 8px #0008';
                        document.body.prepend(el);
                    }
                    el.textContent = 'CineBot · Payment #' + index + ' · ' + state;
                }""",
                {"index": session.index, "state": state},
            )
        except Exception:
            pass

    async def _wait_for_seat(self, page, label: str):
        """Find a rendered seat despite Cineplex changing its seat DOM shape.

        Order matters: the live site exposes seats as elements whose exact text
        is the label, so try that first with a short timeout. The old
        data-seat-first order burned 5s per seat waiting on a selector that
        never exists on this site.
        """
        selectors = (
            page.get_by_text(label, exact=True).first,
            page.locator(f'a:has-text("{label}")').first,
            page.locator(f'[data-seat="{label}"]').first,
        )
        last_error: Exception | None = None
        for locator, timeout in candidates:
            try:
                await locator.wait_for(state="visible", timeout=1_500)
                return locator
            except Exception as exc:
                last_error = exc
        raise GroupPlanError(
            f"Seat {label} is no longer visible in the live seat map; "
            "reload the show to refresh availability."
        ) from last_error

    async def _wait_enabled(self, locator, *, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1_000
        await locator.wait_for(state="visible", timeout=timeout_ms)
        while time.monotonic() < deadline:
            if await locator.is_enabled():
                return
            await asyncio.sleep(0.1)
        raise GroupPlanError("Cineplex did not enable Purchase in time.")

    async def _dismiss_swal2(self, page) -> None:
        try:
            btns = page.locator(".swal2-confirm")
            if await btns.count() > 0 and await btns.first.is_visible():
                await btns.first.click(timeout=1500)
                await page.wait_for_timeout(120)
                if await page.locator(".swal2-confirm").count() > 0:
                    await page.locator(".swal2-confirm").first.click(timeout=1000)
        except Exception:
            pass

    def _set_session(self, session: PaymentSession, status: str, detail: str) -> None:
        session.status = status
        session.detail = detail
        if status not in {"waiting_otp", "submitting_otp", "manual_payment"}:
            session.otp_required = False
        if status not in {"pin_required", "manual_payment"}:
            session.pin_required = False

    def _fail_session(self, session: PaymentSession, detail: str) -> None:
        session.status = "failed"
        session.detail = detail
        session.error = detail
        session.otp_required = False
        session.pin_required = False
