"""Minimal local control UI for the Hall 6 Spider-Man group booking."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..group import (
    TARGET_DATE,
    TARGET_HALL,
    TARGET_LOCATION,
    TARGET_MOVIE,
    TARGET_ROWS,
    GroupPlanError,
)
from ..live.group_run import GroupBookingManager

log = logging.getLogger("cinebot.ui")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class StartIn(BaseModel):
    bkash_number: str = Field(..., min_length=11, max_length=16)
    names: list[str] = Field(..., min_length=4, max_length=4)


class OtpIn(BaseModel):
    session_id: str = Field(..., min_length=4, max_length=64)
    code: str = Field(..., min_length=4, max_length=8)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.group = GroupBookingManager()
    log.info("Hall 6 group booking console ready")
    try:
        yield
    finally:
        manager: GroupBookingManager = app.state.group
        if manager.busy:
            await manager.stop()


app = FastAPI(
    title="Hall 6 Group Booker",
    description="Local-only Spider-Man group booking console",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _manager(request: Request) -> GroupBookingManager:
    return request.app.state.group


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/api/group/config")
def group_config():
    return {
        "movie": TARGET_MOVIE,
        "date": TARGET_DATE,
        "location": TARGET_LOCATION,
        "hall": TARGET_HALL,
        "time_window": "4:00–6:00 PM",
        "rows": list(TARGET_ROWS),
        "expected_seats": 34,
        "expected_payments": 4,
        "expected_chunks": [
            {"row": "E", "seats": "E1–E10", "count": 10},
            {"row": "E", "seats": "E11–E17", "count": 7},
            {"row": "F", "seats": "F1–F10", "count": 10},
            {"row": "F", "seats": "F11–F17", "count": 7},
        ],
        "pin_policy": "PIN is entered only in the secure bKash window.",
    }


@app.get("/api/group/state")
def group_state(request: Request):
    return _manager(request).snapshot()


@app.post("/api/group/start")
async def group_start(body: StartIn, request: Request):
    manager = _manager(request)
    try:
        run_id = await manager.start(body.bkash_number, body.names)
    except GroupPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"run_id": run_id}, status_code=202)


@app.post("/api/group/otp")
def group_otp(body: OtpIn, request: Request):
    manager = _manager(request)
    try:
        manager.submit_otp(body.session_id, body.code)
    except GroupPlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"accepted": True}


@app.post("/api/group/stop")
async def group_stop(request: Request):
    return {"stopped": await _manager(request).stop()}


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run("cinebot.ui.app_v2:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()

