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


def payments_from_payload(payloads: list[dict[str, Any]]) -> list[PaymentRequest]:
    if len(payloads) != 4:
        raise GroupPlanError("Exactly four payment sessions are required.")
    names = validate_names([str(item.get("name") or "") for item in payloads], 4)
    phones = [
        validate_bkash_number(str(item.get("bkash_number") or ""))
        for item in payloads
    ]
    if len(set(phones)) != 4:
        raise GroupPlanError("Use four different bKash numbers, one per session.")

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
    """Own one four-payment booking run for the local control page."""

    def __init__(self) -> None:
        self.status = "idle"
        self.phase = "Ready"
        self.detail = "Load a show, choose seats, then enter four payment details."
        self.error: Optional[str] = None
        self.show: Optional[dict[str, Any]] = None
        self.sessions: dict[str, PaymentSession] = {}
        self.started_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._browser = None
        self._ready_event: asyncio.Event | None = None
        self._ready_lock: asyncio.Lock | None = None
        self._ready_count = 0
        self._ready_expected = 0
        self._gate_error: Optional[str] = None
        self._purchases_released = False

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
        }

    async def start(
        self, target_payload: dict[str, Any], payment_payloads: list[dict[str, Any]]
    ) -> str:
        if self.busy:
            raise GroupPlanError("A group booking is already running.")
        target = target_from_payload(target_payload)
        payments = payments_from_payload(payment_payloads)
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
        run_id = f"group_{uuid.uuid4().hex[:10]}"
        self._task = asyncio.create_task(self._run(run_id, target, payments), name=run_id)
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
                self.phase = "Preparing four sessions"
                self.detail = "Opening four isolated sessions and verifying exact seats."
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
                    self.detail = "All four payment sessions returned successfully."
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
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
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
        button = page.locator("button.guest-login").first
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
        page = await context.new_page()
        try:
            self._set_session(session, "opening", "Opening a private session...")
            await self._guest_login(page)
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

            plus = page.locator(".ticket_qty_view div:nth-child(3) img").first
            await plus.wait_for(state="visible", timeout=10_000)
            for _ in session.chunk.seats:
                await plus.click()
                await page.wait_for_timeout(200)

            # The Purchase button doubles as the control that reveals the seat
            # map. Click it once (reCAPTCHA must have resolved to enable it) so
            # the seat cells become clickable, matching the proven run.py flow.
            purchase = page.locator("button.btn-desktop-purchase").first
            await self._wait_enabled(purchase, timeout_ms=30_000)
            await self._dismiss_swal2(page)
            await purchase.click()
            await page.wait_for_timeout(2000)

            await page.get_by_text(session.chunk.labels[0], exact=True).first.wait_for(
                state="visible", timeout=15_000
            )
            for label in session.chunk.labels:
                # a SweetAlert2 popup ("Do you want to allow...") can intercept the
                # first seat click and silently drop it; dismiss before every click.
                await self._dismiss_swal2(page)
                await page.get_by_text(label, exact=True).first.click(timeout=5_000)

            selected_text = await page.locator(".selected_seat").inner_text(timeout=5_000)
            accepted = set(re.findall(r"\b[A-Z]{1,3}\d+\b", selected_text.upper()))
            missing = [label for label in session.chunk.labels if label not in accepted]
            if missing:
                raise GroupPlanError(
                    "Cineplex did not accept assigned seats: " + ", ".join(missing)
                )

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
            await self._wait_for_group_release(session)
            self._set_session(session, "booking", "Creating the Cineplex booking...")
            async with page.expect_response(
                lambda response: re.search(r"/api/v1/booking$", response.url) is not None,
                timeout=30_000,
            ) as booking_response_info:
                await purchase.click()
            booking_payload = await (await booking_response_info.value).json()
            if booking_payload.get("code") != 200:
                messages = booking_payload.get("message") or ["Booking rejected"]
                raise GroupPlanError(str(messages[0]))

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
            await confirm.click(timeout=8_000)

            otp_input = page.locator(
                "input[placeholder*='6 digit' i], input[placeholder*='code' i], "
                "input[name*='otp' i], input[id*='otp' i]"
            ).first
            await otp_input.wait_for(state="visible", timeout=25_000)
            session._otp_future = asyncio.get_running_loop().create_future()
            self._set_session(
                session,
                "waiting_otp",
                "Enter the OTP from this payment's bKash SMS.",
            )
            session.otp_required = True
            code = await asyncio.wait_for(session._otp_future, timeout=180)
            await otp_input.fill(code)
            code = ""
            otp_confirm = page.locator(
                "button:has-text('Confirm'), [role=button]:has-text('Confirm'), "
                "a:has-text('Confirm')"
            ).last
            await otp_confirm.click(timeout=8_000)

            pin_input = page.locator(
                "input[type='password'], input[placeholder*='PIN' i], "
                "input[name*='pin' i], input[id*='pin' i]"
            ).first
            await pin_input.wait_for(state="visible", timeout=25_000)
            self._set_session(
                session,
                "pin_required",
                "OTP accepted. Enter the PIN only in this secure bKash window.",
            )
            session.pin_required = True
            await self._wait_for_payment_result(page, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._purchases_released:
                await self._abort_group_gate(str(exc))
            self._fail_session(session, str(exc))
        finally:
            if session._otp_future is not None and not session._otp_future.done():
                session._otp_future.cancel()
            await context.close()

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
            if self._ready_count == self._ready_expected:
                self._purchases_released = True
                self.phase = "All four ready"
                self.detail = "All seats verified. Releasing four purchases together."
                self._ready_event.set()
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=45)
        except TimeoutError as exc:
            await self._abort_group_gate("Not all four sessions became ready in time.")
            raise GroupPlanError("Not all four sessions became ready in time.") from exc
        if self._gate_error:
            raise GroupPlanError(
                "Purchases stopped before booking because another session failed: "
                + self._gate_error
            )

    async def _abort_group_gate(self, reason: str) -> None:
        if self._purchases_released or self._ready_event is None:
            return
        assert self._ready_lock is not None
        async with self._ready_lock:
            if not self._gate_error:
                self._gate_error = reason
            self._ready_event.set()

    async def _wait_for_payment_result(
        self, page, session: PaymentSession, timeout_seconds: int = 600
    ) -> None:
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
            if "payment failed" in text or "transaction failed" in text:
                raise GroupPlanError("bKash reported that the payment failed.")
            await page.wait_for_timeout(1_000)
        raise GroupPlanError("Timed out waiting for bKash payment confirmation.")

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

    async def _wait_enabled(self, locator, *, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1_000
        await locator.wait_for(state="visible", timeout=timeout_ms)
        while time.monotonic() < deadline:
            if await locator.is_enabled():
                return
            await asyncio.sleep(0.1)
        raise GroupPlanError("Cineplex did not enable Purchase in time.")

    async def _dismiss_swal2(self, page) -> None:
        """Dismiss SweetAlert2 popups that intercept pointer events."""
        for _ in range(2):
            try:
                btn = page.locator(".swal2-confirm").first
                if await btn.is_visible(timeout=400):
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(150)
                    continue
            except Exception:
                pass
            return

    def _set_session(self, session: PaymentSession, status: str, detail: str) -> None:
        session.status = status
        session.detail = detail
        if status not in {"waiting_otp", "submitting_otp"}:
            session.otp_required = False
        if status != "pin_required":
            session.pin_required = False

    def _fail_session(self, session: PaymentSession, detail: str) -> None:
        session.status = "failed"
        session.detail = detail
        session.error = detail
        session.otp_required = False
        session.pin_required = False
