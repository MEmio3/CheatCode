"""Local UI and API for the live Cineplex picker and four payment sessions."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..group import GroupPlanError
from ..live.catalog import CatalogError, CatalogManager
from ..live.group_booking import GroupBookingManager

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
    payments: list[PaymentIn] = Field(..., min_length=4, max_length=4)


class OtpIn(BaseModel):
    session_id: str = Field(..., min_length=4, max_length=64)
    code: str = Field(..., min_length=4, max_length=8)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.catalog = CatalogManager()
    app.state.group = GroupBookingManager()
    log.info("Live Cineplex group picker ready")
    try:
        yield
    finally:
        manager: GroupBookingManager = app.state.group
        if manager.busy:
            await manager.stop()


app = FastAPI(
    title="Cineplex Group Booker",
    description="Local live show picker with four synchronized bKash sessions",
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


@app.get("/legacy")
def legacy():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/api/group/config")
def group_config():
    return {
        "payments": 4,
        "max_seats_per_payment": 10,
        "max_total_seats": 40,
        "pin_policy": "PIN is entered only in the secure bKash window.",
    }


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


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run("cinebot.ui.app_v2:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
