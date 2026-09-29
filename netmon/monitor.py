"""Status logic and the polling loop.

The status rules (apply_check, restore_state) are pure functions with no I/O,
so they are easy to test and can be reused by the Phase 2 agent. The Monitor
class wires them to the checker and the database.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum

from .checker import CheckResult, run_check
from .db import Database, DeviceRow

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 30
FAILURE_THRESHOLD = 3  # consecutive failures before a device is DOWN
RETENTION_S = 7 * 24 * 3600
RETENTION_RUN_EVERY_S = 3600


class Status(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class Event(str, Enum):
    WENT_DOWN = "went_down"  # open an incident
    CAME_UP = "came_up"      # close the open incident


@dataclass(frozen=True)
class DeviceState:
    status: Status = Status.UNKNOWN
    consecutive_failures: int = 0
    first_failure_at: float | None = None  # start of the current failure streak


# ---------------------------------------------------------------- pure logic

def apply_check(
    state: DeviceState, is_up: bool, at: float, threshold: int = FAILURE_THRESHOLD
) -> tuple[DeviceState, Event | None]:
    """Return the new state after one check, plus an incident event if any.

    - Success: UP immediately, failure count reset. Closes an incident if DOWN.
    - Failure: count it. DOWN only once the count reaches `threshold`; before
      that the previous status is kept (UP stays UP, UNKNOWN stays UNKNOWN).
    """
    if is_up:
        event = Event.CAME_UP if state.status is Status.DOWN else None
        return DeviceState(Status.UP, 0, None), event

    failures = state.consecutive_failures + 1
    first = state.first_failure_at if state.first_failure_at is not None else at
    if failures >= threshold:
        event = Event.WENT_DOWN if state.status is not Status.DOWN else None
        return DeviceState(Status.DOWN, failures, first), event
    return DeviceState(state.status, failures, first), None


def restore_state(
    open_incident_started_at: float | None,
    recent_checks: list[tuple[float, bool]],
    now: float,
    max_age_s: float,
    threshold: int = FAILURE_THRESHOLD,
) -> DeviceState:
    """Rebuild a device's state after a restart.

    recent_checks: newest-first (timestamp, is_up).
    - An open incident means the device is DOWN (continue that incident rather
      than opening a duplicate).
    - Otherwise, only trust history newer than max_age_s; anything older is
      stale, so the device starts UNKNOWN with a clean slate.
    """
    if open_incident_started_at is not None:
        trailing = 0
        for _, ok in recent_checks:
            if ok:
                break
            trailing += 1
        return DeviceState(Status.DOWN, max(trailing, threshold), open_incident_started_at)

    if not recent_checks or now - recent_checks[0][0] > max_age_s:
        return DeviceState()

    trailing = 0
    first_failure_at = None
    status = Status.UNKNOWN
    for ts, ok in recent_checks:
        if ok:
            status = Status.UP
            break
        trailing += 1
        first_failure_at = ts
    # Without an open incident the device was never marked DOWN, so cap the
    # streak one short of the threshold: the next failure will open an incident.
    trailing = min(trailing, threshold - 1)
    return DeviceState(status, trailing, first_failure_at if trailing else None)


# ---------------------------------------------------------------- runtime

@dataclass
class DeviceRuntime:
    device: DeviceRow
    state: DeviceState
    last_checked: float | None = None
    last_latency_ms: float | None = None
    last_is_up: bool | None = None


class Monitor:
    def __init__(
        self,
        db: Database,
        devices: list[DeviceRow],
        interval_s: float = CHECK_INTERVAL_S,
        threshold: int = FAILURE_THRESHOLD,
    ):
        self.db = db
        self.interval_s = interval_s
        self.threshold = threshold
        self.devices: dict[int, DeviceRuntime] = {}
        now = time.time()
        max_age = interval_s * (threshold + 2)
        for d in devices:
            recent = db.recent_checks(d.id, threshold + 1)
            state = restore_state(
                db.open_incident_start(d.id), [(ts, ok) for ts, ok, _ in recent],
                now, max_age, threshold,
            )
            rt = DeviceRuntime(device=d, state=state)
            if recent:
                rt.last_checked, rt.last_is_up, rt.last_latency_ms = recent[0]
            self.devices[d.id] = rt

    def record(self, device_id: int, result: CheckResult, at: float) -> Event | None:
        """Apply one check result: persist it, then update in-memory state.

        The DB write happens first; if it fails, memory is left unchanged so
        the transition is retried on the next check instead of being lost.
        """
        rt = self.devices[device_id]
        new_state, event = apply_check(rt.state, result.is_up, at, self.threshold)
        self.db.record_check(
            device_id, at, result.is_up, result.latency_ms,
            open_incident_at=new_state.first_failure_at if event is Event.WENT_DOWN else None,
            close_incident_at=at if event is Event.CAME_UP else None,
        )
        rt.state = new_state
        rt.last_checked = at
        rt.last_is_up = result.is_up
        rt.last_latency_ms = result.latency_ms
        if event is Event.WENT_DOWN:
            log.warning("%s is DOWN (%d failed checks)", rt.device.name, new_state.consecutive_failures)
        elif event is Event.CAME_UP:
            log.info("%s is back UP", rt.device.name)
        return event

    async def check_all(self) -> None:
        """Check every device concurrently, then record the results."""
        runtimes = list(self.devices.values())
        at = time.time()
        results = await asyncio.gather(
            *(run_check(rt.device.check_method, rt.device.ip, rt.device.port) for rt in runtimes),
            return_exceptions=True,
        )
        for rt, result in zip(runtimes, results):
            if isinstance(result, BaseException):
                # A bug, not a network failure: don't count it against the device.
                log.error("check crashed for %s: %r", rt.device.name, result)
                continue
            try:
                self.record(rt.device.id, result, at)
            except Exception:
                log.exception("failed to record check for %s", rt.device.name)

    async def run(self) -> None:
        """Check all devices every interval, forever."""
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.check_all()
            except Exception:
                log.exception("check cycle failed")
            await asyncio.sleep(max(0.0, self.interval_s - (loop.time() - started)))

    async def run_retention(self) -> None:
        """Delete raw checks older than 7 days, once an hour."""
        while True:
            try:
                deleted = await asyncio.to_thread(
                    self.db.delete_checks_before, time.time() - RETENTION_S
                )
                if deleted:
                    log.info("retention: deleted %d old checks", deleted)
            except Exception:
                log.exception("retention cleanup failed")
            await asyncio.sleep(RETENTION_RUN_EVERY_S)

    def snapshot(self) -> list[DeviceRuntime]:
        return list(self.devices.values())
