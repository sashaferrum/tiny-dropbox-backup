"""Foreground scheduler with one sequential worker and bounded pending work."""

from __future__ import annotations

import logging
import threading
import time as time_module
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from .config import JobConfig

logger = logging.getLogger(__name__)


def advance_interval(due: float, now: float, interval: int) -> float:
    """Return the first startup-anchored grid point strictly after now."""
    missed = int((now - due) // interval) + 1
    return due + missed * interval


def daily_due_jobs(
    jobs: tuple[JobConfig, ...],
    current: datetime,
    previous_minute: tuple[int, int, int, int, int],
    started_wall: datetime,
    executed: set[tuple[str, str, object]],
) -> tuple[list[JobConfig], tuple[int, int, int, int, int]]:
    """Return daily jobs matching this newly observed local minute."""
    minute_key = _minute_key(current)
    if minute_key == previous_minute:
        return [], previous_minute
    due: list[JobConfig] = []
    for job in jobs:
        if job.schedule.mode != "daily":
            continue
        for configured_time in job.schedule.daily_times:
            if current.hour == configured_time.hour and current.minute == configured_time.minute:
                token = (job.name, configured_time.isoformat(), current.date())
                if token not in executed and current.timestamp() > started_wall.timestamp():
                    due.append(job)
                    executed.add(token)
    return due, minute_key


def run_scheduler(
    jobs: tuple[JobConfig, ...],
    timezone,
    run_job: Callable[[JobConfig], object],
    shutdown: threading.Event,
    *,
    monotonic: Callable[[], float] = time_module.monotonic,
    wall_now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
) -> None:
    """Run due jobs sequentially; missed interval boundaries never accumulate."""
    if wall_now is None:
        wall_now = lambda: datetime.now(timezone)
    started_mono = monotonic()
    started_wall = wall_now()
    interval_due = {
        job.name: started_mono
        for job in jobs
        if job.schedule.mode == "interval"
    }
    daily_executed: set[tuple[str, str, object]] = set()
    last_daily_minute = _minute_key(started_wall)
    pending: list[JobConfig] = []
    pending_names: set[str] = set()
    running: JobConfig | None = None
    active: Future | None = None

    def enqueue(job: JobConfig) -> None:
        if (running is not None and running.name == job.name) or job.name in pending_names:
            return
        pending.append(job)
        pending_names.add(job.name)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mirror-job") as worker:
        while True:
            if shutdown.is_set():
                pending.clear()
                pending_names.clear()

            now_mono = monotonic()
            now_wall = wall_now()
            due_daily, last_daily_minute = daily_due_jobs(
                jobs, now_wall, last_daily_minute, started_wall, daily_executed
            )
            daily_names = {job.name for job in due_daily}
            for job in jobs:
                schedule = job.schedule
                if schedule.mode == "interval":
                    due = interval_due[job.name]
                    if now_mono >= due:
                        enqueue(job)
                        interval = schedule.interval_seconds or 1
                        interval_due[job.name] = advance_interval(due, now_mono, interval)
                elif not shutdown.is_set() and job.name in daily_names:
                    enqueue(job)

            if active is not None and active.done():
                try:
                    active.result()
                except Exception:
                    logger.exception("Scheduled job %s terminated unexpectedly", running.name if running else "")
                active = None
                running = None

            if active is None and pending and not shutdown.is_set():
                running = pending.pop(0)
                pending_names.remove(running.name)
                active = worker.submit(run_job, running)

            if shutdown.is_set():
                if active is None:
                    return
                # The current job owns its file-level retry budget; do not dispatch pending work.
                sleep(0.1)
                continue
            sleep(0.25)


def _minute_key(value: datetime) -> tuple[int, int, int, int, int]:
    return value.year, value.month, value.day, value.hour, value.minute
