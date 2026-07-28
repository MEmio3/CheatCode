"""Shared step taxonomy and event types for the run state machine.

Emitted by the runner, streamed to the UI over SSE, and inspected by the OTP
router to decide which OTP label to show.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Phase(str, Enum):
    IDLE = "idle"
    LOGIN = "login"
    BROWSE = "browse"
    SEATS = "seats"
    HOLD = "hold"
    ORDER = "order"
    PAYMENT = "payment"
    CONFIRM = "confirm"
    DONE = "done"
    ERROR = "error"


class OtpPurpose(str, Enum):
    CINEPLEX_LOGIN = "cineplex_login"
    BKASH_PAYMENT = "bkash_payment"


# Human description of each OTP purpose, shown in the UI prompt card.
OTP_LABELS: dict[OtpPurpose, str] = {
    OtpPurpose.CINEPLEX_LOGIN: "Cineplex login OTP",
    OtpPurpose.BKASH_PAYMENT: "bKash payment authorization OTP",
}

# Where each OTP arrives, so the UI can tell the user which app to check.
OTP_SOURCE: dict[OtpPurpose, str] = {
    OtpPurpose.CINEPLEX_LOGIN: "SMS from STAR Cineplex",
    OtpPurpose.BKASH_PAYMENT: "SMS from bKash",
}


class StepEvent(BaseModel):
    step: str  # machine id, e.g. "login.waiting_otp"
    label: str  # human label, e.g. "Waiting for login OTP"
    phase: Phase
    progress: float = Field(ge=0.0, le=1.0)
    detail: Optional[str] = None
    otp: Optional[OtpPurpose] = None  # present only when an OTP is being requested
    otp_channel: Optional[str] = None  # "manual" | "telegram"
    is_terminal: bool = False  # True on done / fatal error
    session_id: Optional[str] = None  # which session/account this event belongs to
    ts: float


# Ordered step catalog. (id, label, phase, progress). OTP steps carry their
# purpose so the runner/UI agree on what's being asked.
STEPS: list[tuple[str, str, Phase, float, Optional[OtpPurpose]]] = [
    ("session.restore", "Restoring session", Phase.LOGIN, 0.05, None),
    ("login.request_otp", "Requesting login OTP", Phase.LOGIN, 0.10, None),
    ("login.waiting_otp", "Waiting for login OTP", Phase.LOGIN, 0.18, OtpPurpose.CINEPLEX_LOGIN),
    ("browse.find_movie", "Finding movie", Phase.BROWSE, 0.30, None),
    ("browse.showtimes", "Listing showtimes", Phase.BROWSE, 0.40, None),
    ("seats.load_map", "Loading seat map", Phase.SEATS, 0.50, None),
    ("seats.select", "Selecting seats", Phase.SEATS, 0.58, None),
    ("hold.seats", "Holding seats", Phase.HOLD, 0.65, None),
    ("order.create", "Creating order", Phase.ORDER, 0.70, None),
    ("payment.init", "Initiating payment", Phase.PAYMENT, 0.78, None),
    ("payment.waiting_otp", "Waiting for bKash payment OTP", Phase.PAYMENT, 0.88, OtpPurpose.BKASH_PAYMENT),
    ("payment.confirm", "Confirming payment", Phase.CONFIRM, 0.95, None),
    ("done", "Booking confirmed", Phase.DONE, 1.00, None),
]

_BY_ID: dict[str, tuple[str, str, Phase, float, Optional[OtpPurpose]]] = {s[0]: s for s in STEPS}


def make_event(
    step_id: str,
    *,
    detail: Optional[str] = None,
    otp_channel: Optional[str] = None,
    is_terminal: bool = False,
    ts: float = 0.0,
) -> StepEvent:
    """Build a StepEvent from the catalog. Raises KeyError on unknown step ids."""
    _, label, phase, progress, otp = _BY_ID[step_id]
    return StepEvent(
        step=step_id,
        label=label,
        phase=phase,
        progress=progress,
        detail=detail,
        otp=otp,
        otp_channel=otp_channel,
        is_terminal=is_terminal or phase in (Phase.DONE, Phase.ERROR),
        ts=ts,
    )


def error_event(detail: str, ts: float = 0.0) -> StepEvent:
    return StepEvent(
        step="error",
        label="Booking failed",
        phase=Phase.ERROR,
        progress=0.0,
        detail=detail,
        is_terminal=True,
        ts=ts,
    )


def run_start_event(ts: float = 0.0) -> StepEvent:
    """Boundary event published at the start of each run so the UI can reset
    its step list + result line before the first real step streams in."""
    return StepEvent(
        step="run.start",
        label="Run started",
        phase=Phase.IDLE,
        progress=0.0,
        ts=ts,
    )
