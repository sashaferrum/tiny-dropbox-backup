import threading
import time
from datetime import datetime, time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dropbox_mirror.config import JobConfig, Schedule
from dropbox_mirror.scheduler import advance_interval, daily_due_jobs, run_scheduler


def job(name, schedule):
    return JobConfig(name, "directory", Path("/unused"), f"/{name}", schedule, ())


def test_interval_grid_is_startup_anchored_and_skips_missed_boundaries():
    assert advance_interval(100.0, 100.0, 10) == 110.0
    assert advance_interval(100.0, 145.0, 10) == 150.0
    assert advance_interval(100.0, 1000.0, 10) == 1010.0


def test_all_startup_interval_jobs_run_immediately_in_ini_order():
    shutdown = threading.Event()
    seen = []
    jobs = (
        job("first", Schedule("interval", interval_seconds=3600)),
        job("second", Schedule("interval", interval_seconds=3600)),
    )

    def run(current):
        seen.append(current.name)
        if len(seen) == 2:
            shutdown.set()

    run_scheduler(
        jobs,
        ZoneInfo("UTC"),
        run,
        shutdown,
        wall_now=lambda: datetime(2025, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC")),
        sleep=lambda seconds: time.sleep(0.002),
    )

    assert seen == ["first", "second"]


def test_only_one_pending_interval_run_is_kept():
    shutdown = threading.Event()
    first_started = threading.Event()
    release_first = threading.Event()
    seen = []
    jobs = (job("frequent", Schedule("interval", interval_seconds=1)),)
    clock = [0.0]
    loops = [0]

    def run(current):
        seen.append(current.name)
        if len(seen) == 1:
            first_started.set()
            assert release_first.wait(2)
        else:
            shutdown.set()

    def sleep(seconds):
        clock[0] += 0.25
        loops[0] += 1
        if loops[0] == 20:
            release_first.set()
        time.sleep(0.001)

    run_scheduler(
        jobs,
        ZoneInfo("UTC"),
        run,
        shutdown,
        monotonic=lambda: clock[0],
        wall_now=lambda: datetime(2025, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC")),
        sleep=sleep,
    )

    assert first_started.is_set()
    assert len(seen) == 2


def test_daily_schedule_starts_at_next_future_occurrence():
    timezone = ZoneInfo("UTC")
    current_job = job("daily", Schedule("daily", daily_times=(datetime_time(10, 0),)))
    start = datetime(2025, 1, 1, 10, 0, tzinfo=timezone)
    executed = set()
    due, key = daily_due_jobs((current_job,), start, (2025, 1, 1, 9, 59), start, executed)
    assert due == []
    assert key == (2025, 1, 1, 10, 0)

    next_day = datetime(2025, 1, 2, 10, 0, tzinfo=timezone)
    due, _ = daily_due_jobs((current_job,), next_day, key, start, executed)
    assert [item.name for item in due] == ["daily"]


def test_daily_jobs_due_together_keep_ini_order():
    timezone = ZoneInfo("UTC")
    jobs = (
        job("first", Schedule("daily", daily_times=(datetime_time(10, 0),))),
        job("second", Schedule("daily", daily_times=(datetime_time(10, 0),))),
    )
    start = datetime(2025, 1, 1, 9, 0, tzinfo=timezone)
    now = datetime(2025, 1, 1, 10, 0, tzinfo=timezone)
    due, _ = daily_due_jobs(jobs, now, (2025, 1, 1, 9, 59), start, set())
    assert [item.name for item in due] == ["first", "second"]


def test_fall_back_local_time_runs_at_most_once_per_day():
    try:
        timezone = ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("IANA time-zone database is unavailable")
    current_job = job("daily", Schedule("daily", daily_times=(datetime_time(1, 30),)))
    start = datetime(2024, 11, 3, 0, 0, tzinfo=timezone)
    first = datetime(2024, 11, 3, 1, 30, tzinfo=timezone, fold=0)
    second = datetime(2024, 11, 3, 1, 30, tzinfo=timezone, fold=1)
    executed = set()
    due, _ = daily_due_jobs((current_job,), first, (2024, 11, 3, 1, 29), start, executed)
    assert len(due) == 1
    due, _ = daily_due_jobs((current_job,), second, (2024, 11, 3, 1, 59), start, executed)
    assert due == []


def test_spring_forward_nonexistent_local_time_is_skipped():
    try:
        timezone = ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("IANA time-zone database is unavailable")
    current_job = job("daily", Schedule("daily", daily_times=(datetime_time(2, 30),)))
    start = datetime(2024, 3, 10, 0, 0, tzinfo=timezone)
    after_jump = datetime(2024, 3, 10, 3, 0, tzinfo=timezone)
    due, _ = daily_due_jobs((current_job,), after_jump, (2024, 3, 10, 1, 59), start, set())
    assert due == []
