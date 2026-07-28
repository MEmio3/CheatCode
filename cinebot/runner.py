"""Run orchestrator: owns the bus, OTP broker, timer, and the active run.

One Runner = one booking at a time (matches the single-account guardrail). The
UI holds a single instance; starts a run in the background, streams events over
SSE, and forwards user-typed OTPs back through submit_otp().
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .events import EventBus, OtpBroker
from .flow import Flow, FlowContext, RunConfig, RunResult
from .otp.telegram import TelegramRelay
from .recover import NoopRecover, Recover
from .steps import run_start_event
from .timer import HoldTimer

log = logging.getLogger("cinebot.runner")


@dataclass
class RunHandle:
    run_id: str
    task: "asyncio.Task"


class RunnerBusy(Exception):
    """A booking is already in progress."""


class Runner:
    def __init__(
        self,
        flow: Flow,
        *,
        telegram: Optional[TelegramRelay] = None,
        recover: Optional[Recover] = None,
        clock: Callable[[], float] = time.time,
        session_id: Optional[str] = None,
    ):
        self.flow = flow
        self.session_id = session_id
        self.bus = EventBus(clock=clock, session_id=session_id)
        self.broker = OtpBroker(self.bus, telegram=telegram, clock=clock)
        self.timer = HoldTimer(self.bus, clock=clock)
        self.recover = recover or NoopRecover()
        self.clock = clock
        self._current: Optional[RunHandle] = None
        self._lock = asyncio.Lock()
        self._last_result: Optional[RunResult] = None

    @property
    def last_result(self) -> Optional[RunResult]:
        return self._last_result

    @property
    def is_busy(self) -> bool:
        return self._current is not None and not self._current.task.done()

    async def start(self, config: RunConfig) -> str:
        async with self._lock:
            if self.is_busy:
                raise RunnerBusy("a booking is already running")
            # fresh event history per run; emit a boundary event so the UI resets
            self.bus.history.clear()
            await self.bus.publish(run_start_event())
            run_id = f"run_{int(self.clock())}"
            ctx = FlowContext(
                bus=self.bus,
                otp=self.broker,
                timer=self.timer,
                recover=self.recover,
                clock=self.clock,
            )
            task = asyncio.create_task(self._run(config, ctx, run_id), name=run_id)
            self._current = RunHandle(run_id=run_id, task=task)
            return run_id

    async def _run(self, config: RunConfig, ctx: FlowContext, run_id: str) -> None:
        try:
            result = await self.flow.run(config, ctx)
            self._last_result = result
            log.info("run %s finished ok=%s", run_id, result.ok)
        except Exception as e:
            log.exception("run %s crashed", run_id)
            self._last_result = RunResult(ok=False, error=str(e))
        finally:
            self._current = None

    def submit_otp(self, req_id: str, code: str) -> bool:
        return self.broker.submit(req_id, code)

    async def cancel(self) -> bool:
        if self._current is None:
            return False
        self._current.task.cancel()
        return True
