"""Local UI and API for the live Cineplex picker and payment sessions."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config_store import CredentialStore, telegram_env_credentials
from ..group import GroupPlanError
from ..live.catalog import CatalogError, CatalogManager
from ..live.group_booking import GroupBookingManager
from ..sniper import SnipeAttendee, SnipeConfig, SniperManager, is_active, load_config, save_config

log = logging.getLogger("cinebot.ui")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class TargetIn(BaseModel):
    location_id: int = Field(..., gt=0)
    location_name: str = Field(..., min_length=1, max_length=160)
    show_date: str = Field(..., min_length=10, max_length=10)
    movie_id: int = Field(..., gt=0)
    movie_title: str = Field(..., min_length=1, max_length=200)
    program_id: int = Field(..., gt=0)
    screen_id: int = Field(..., gt=0)
    hall_name: str = Field(..., min_length=1, max_length=80)
    show_time: str = Field(..., min_length=4, max_length=12)
    seat_type_id: int = Field(..., gt=0)
    seat_type_name: str = Field(..., min_length=1, max_length=80)
    unit_price: int = Field(..., ge=0)


class PaymentIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    bkash_number: str = Field(..., min_length=11, max_length=16)
    seats: list[str] = Field(..., min_length=1, max_length=10)


class StartIn(BaseModel):
    target: TargetIn
    payments: list[PaymentIn] = Field(..., min_length=1, max_length=8)
    allow_duplicate_identity: bool = False
    fast: bool = False


class ApiProbeIn(BaseModel):
    target: TargetIn
    payment: PaymentIn


class OtpIn(BaseModel):
    session_id: str = Field(..., min_length=4, max_length=64)
    code: str = Field(..., min_length=4, max_length=8)


class SnipeAttendeeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    bkash: str = Field(..., min_length=11, max_length=16)


class SnipeConfigIn(BaseModel):
    target_movie: str = Field(..., min_length=1, max_length=200)
    location_id: int = Field(0, ge=0)
    location_name: str = Field("", max_length=160)
    all_locations: bool = False
    hall_ids: list[int] = Field(default_factory=list, max_length=12)
    show_date: str = Field(..., min_length=10, max_length=10)
    time_start: str = Field("", max_length=5)
    time_end: str = Field("", max_length=5)
    poll_seconds: int = Field(75, ge=15, le=600)
    total_seats: int = Field(1, ge=1, le=40)
    primary_rows: list[str] = Field(default_factory=list, max_length=26)
    fill_row: str = Field("", max_length=3)
    tolerance: int = Field(3, ge=1, le=6)
    force: bool = False
    num_payments: int = Field(1, ge=1, le=8)
    attendees: list[SnipeAttendeeIn] = Field(..., min_length=1, max_length=8)


class TelegramConfigIn(BaseModel):
    bot_token: str = Field(..., min_length=20, max_length=200)
    chat_id: str = Field(..., min_length=1, max_length=40)


class HeadlessIn(BaseModel):
    headless: bool


def _to_snipe_config(body: SnipeConfigIn) -> SnipeConfig:
    return SnipeConfig(
        target_movie=body.target_movie,
        location_id=body.location_id,
        location_name=body.location_name,
        all_locations=body.all_locations,
        hall_ids=list(body.hall_ids),
        show_date=body.show_date,
        time_start=body.time_start,
        time_end=body.time_end,
        poll_seconds=body.poll_seconds,
        total_seats=body.total_seats,
        primary_rows=list(body.primary_rows),
        fill_row=body.fill_row,
        tolerance=body.tolerance,
        force=body.force,
        num_payments=body.num_payments,
        attendees=[
            SnipeAttendee(name=a.name, bkash=a.bkash) for a in body.attendees
        ],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.catalog = CatalogManager()
    app.state.group = GroupBookingManager()
    app.state.sniper = SniperManager(app.state.catalog, app.state.group)
    saved = load_config()
    if saved and is_active():
        log.info("resuming sniper watch for '%s'", saved.target_movie)
        try:
            await app.state.sniper.start(saved)
        except Exception as exc:
            log.warning("sniper resume failed: %s", exc)
    log.info("Live Cineplex group picker ready")
    try:
        yield
    finally:
        manager: GroupBookingManager = app.state.group
        if manager.busy:
            await manager.stop()
        await manager.close_browser()
        if app.state.sniper.busy:
            await app.state.sniper.shutdown()


app = FastAPI(
    title="Cineplex Group Booker",
    description="Local live show picker with synchronized bKash sessions",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _group(request: Request) -> GroupBookingManager:
    return request.app.state.group


def _catalog(request: Request) -> CatalogManager:
    return request.app.state.catalog


def _catalog_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "picker.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve the app icon explicitly so browsers do not log a 404."""
    return FileResponse(
        os.path.join(_STATIC_DIR, "favicon.svg"),
        media_type="image/svg+xml",
    )


@app.get("/legacy")
def legacy():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/payment-status")
def payment_status():
    return FileResponse(os.path.join(_STATIC_DIR, "payment_status.html"))


@app.get("/api/group/config")
def group_config():
    return {
        "payments": 1,
        "max_seats_per_payment": 10,
        "max_total_seats": 40,
        "pin_policy": "PIN is entered only in the secure bKash window.",
    }


@app.get("/api/settings/headless")
def get_headless_setting():
    val = os.getenv("CINEBOT_HEADLESS", "true").lower() not in ("false", "0", "no")
    return {"headless": val}


@app.post("/api/settings/headless")
def set_headless_setting(body: HeadlessIn):
    os.environ["CINEBOT_HEADLESS"] = "true" if body.headless else "false"
    return {"headless": body.headless}


@app.get("/api/catalog/locations")
async def catalog_locations(request: Request):
    try:
        return {"locations": await _catalog(request).locations()}
    except CatalogError as exc:
        raise _catalog_error(exc)


@app.get("/api/catalog/dates/{location_id}")
async def catalog_dates(location_id: int, request: Request):
    try:
        return {"dates": await _catalog(request).dates(location_id)}
    except CatalogError as exc:
        raise _catalog_error(exc)


@app.get("/api/catalog/shows")
async def catalog_shows(
    request: Request,
    location_id: int = Query(..., gt=0),
    movie_id: int = Query(..., gt=0),
    show_date: str = Query(..., min_length=10, max_length=10),
):
    try:
        return {
            "shows": await _catalog(request).shows(
                location_id, movie_id, show_date
            )
        }
    except CatalogError as exc:
        raise _catalog_error(exc)


@app.get("/api/catalog/seats")
async def catalog_seats(
    request: Request,
    location_id: int = Query(..., gt=0),
    program_id: int = Query(..., gt=0),
):
    try:
        return await _catalog(request).seats(location_id, program_id)
    except CatalogError as exc:
        raise _catalog_error(exc)


@app.get("/api/group/state")
def group_state(request: Request):
    return _group(request).snapshot()


@app.post("/api/group/start")
async def group_start(body: StartIn, request: Request):
    manager = _group(request)
    try:
        run_id = await manager.start(
            body.target.model_dump(),
            [payment.model_dump() for payment in body.payments],
            allow_duplicate_identity=body.allow_duplicate_identity,
            fast=body.fast,
        )
    except GroupPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"run_id": run_id}, status_code=202)


@app.post("/api/group/otp")
def group_otp(body: OtpIn, request: Request):
    try:
        _group(request).submit_otp(body.session_id, body.code)
    except GroupPlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"accepted": True}


@app.post("/api/group/stop")
async def group_stop(request: Request):
    return {"stopped": await _group(request).stop()}


@app.post("/api/group/close-browser")
async def group_close_browser(request: Request):
    return {"closed": await _group(request).close_browser()}


@app.post("/api/group/api-probe")
async def group_api_probe(body: ApiProbeIn, request: Request):
    """Probe direct-API /booking: harvest a reCAPTCHA token from a real browser
    and POST /booking over HTTP, returning the raw response. Covers booking only
    (payment still needs a browser). Resolves seat sequence IDs from the live
    layout so the caller only supplies labels."""
    from ..live.api_booking import probe as api_probe
    from ..live.catalog import available_seats_by_label

    catalog = _catalog(request)
    try:
        raw = await catalog.raw_seats(body.target.location_id, body.target.program_id)
    except CatalogError as exc:
        raise _catalog_error(exc)
    available = available_seats_by_label(raw, body.target.seat_type_id)
    seat_seq_ids: list[str] = []
    for label in [str(l).strip().upper() for l in body.payment.seats]:
        cell = available.get(label)
        if cell is None:
            raise HTTPException(
                status_code=400,
                detail=f"Seat {label} is not available in the live layout.",
            )
        seat_seq_ids.append(str(cell["id"]))
    headless = os.getenv("CINEBOT_HEADLESS", "true").lower() not in ("false", "0", "no")
    result = await api_probe(
        body.target.model_dump(),
        body.payment.model_dump(),
        seat_seq_ids,
        headless=headless,
    )
    resp = result.get("response") or {}
    purchase = result.get("purchase") or {}
    log.info(
        "API probe result: ok=%s stage=%s booking http=%s code=%s | "
        "purchase http=%s loc=%s body=%s",
        result.get("ok"),
        result.get("stage"),
        resp.get("http_status"),
        (resp.get("body") or {}).get("code"),
        purchase.get("http_status"),
        purchase.get("location"),
        str(purchase.get("body"))[:600],
    )
    return result


def _sniper(request: Request) -> SniperManager:
    return request.app.state.sniper


@app.get("/api/snipe/config")
def snipe_config_get():
    cfg = load_config()
    return cfg.to_dict() if cfg else {"saved": False}


@app.get("/api/telegram/config")
def telegram_config_get():
    store = CredentialStore.auto()
    env_token, env_chat_id = telegram_env_credentials()
    return {
        "bot_token_set": bool(env_token or store.get("telegram_bot_token")),
        "chat_id": env_chat_id or store.get("telegram_chat_id") or "",
    }


@app.post("/api/telegram/config")
def telegram_config_save(body: TelegramConfigIn):
    store = CredentialStore.auto()
    store.set("telegram_bot_token", body.bot_token.strip())
    store.set("telegram_chat_id", body.chat_id.strip())
    return {"saved": True}


@app.post("/api/snipe/config")
def snipe_config_save(body: SnipeConfigIn):
    cfg = _to_snipe_config(body)
    cfg.validate()
    save_config(cfg)
    return {"saved": True}


@app.post("/api/snipe/start")
async def snipe_start(body: SnipeConfigIn, request: Request):
    sniper = _sniper(request)
    cfg = _to_snipe_config(body)
    try:
        await sniper.start(cfg)
    except GroupPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"started": True}, status_code=202)


@app.post("/api/snipe/stop")
async def snipe_stop(request: Request):
    return {"stopped": await _sniper(request).stop()}


@app.get("/api/snipe/state")
def snipe_state(request: Request):
    return _sniper(request).snapshot()


@app.post("/api/snipe/test")
async def snipe_test(body: SnipeConfigIn, request: Request):
    """Dry-run the sniper against the live schedule without booking."""
    try:
        cfg = _to_snipe_config(body)
        return await _sniper(request).test_config(cfg)
    except GroupPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class _QuietPollFilter(logging.Filter):
    """Hide the high-frequency UI poll lines so real events stay readable."""

    _NOISY = ("/api/group/state", "/api/snipe/state")

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(p in record.getMessage() for p in self._NOISY)


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(_QuietPollFilter())
    uvicorn.run("cinebot.ui.app_v2:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
