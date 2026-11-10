"""Starts a run every night at NIGHTLY_RUN_TIME (in the container's time zone, TZ)."""

import threading
import time
from datetime import datetime, timedelta

from . import runner


def next_nightly_run(nightly_run_time, now=None):
    now = now or datetime.now()
    hour, minute = (int(part) for part in nightly_run_time.split(":"))
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return next_run


def start_nightly_runs(settings):
    def run_every_night():
        while True:
            next_run = next_nightly_run(settings.nightly_run_time)
            # Sleep in short steps, so a changed system clock can't make us miss the run.
            while datetime.now() < next_run:
                time.sleep(min(60, max(1, (next_run - datetime.now()).total_seconds())))
            runner.run(settings, runner.NIGHTLY)

    threading.Thread(target=run_every_night, daemon=True, name="nightly-runs").start()
