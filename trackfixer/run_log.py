"""What happens during a run. Events are saved right away, so the web UI shows progress live."""

import logging

from . import database

logger = logging.getLogger("trackfixer")


class RunLog:
    def __init__(self, run_id):
        self.run_id = run_id
        self.trips_sent = 0
        self.trips_resent_after_app_update = 0
        self.trips_removed = 0
        self.phone_points_deleted = 0
        self.problems = []  # (level, message) of all warnings and errors

    def info(self, message, trip_uid=None):
        self._add("info", message, trip_uid)

    def warning(self, message, trip_uid=None):
        self._add("warning", message, trip_uid)

    def error(self, message, trip_uid=None):
        self._add("error", message, trip_uid)

    @property
    def has_errors(self):
        return any(level == "error" for level, _ in self.problems)

    def _add(self, level, message, trip_uid):
        database.add_run_event(self.run_id, level, message, trip_uid)
        logger.log(logging.INFO if level == "info" else logging.WARNING, "run %s: %s", self.run_id, message)
        if level != "info":
            self.problems.append((level, message))
