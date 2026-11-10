"""The app's own SQLite database: which trainlog trips are in Dawarich, and the run history."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

# What happened to a trainlog trip:
IN_DAWARICH = "in_dawarich"  # its points are in Dawarich
PLANNED = "planned"  # still in the future, will be sent once it's over
SKIPPED = "skipped"  # not a journey (e.g. a restaurant) or no exact time; see status_detail
FAILED = "failed"  # couldn't be converted; see status_detail. Retried when the trip changes.
REMOVED = "removed"  # deleted in trainlog, so its points were removed from Dawarich

# How a run ended:
RUNNING = "running"
OK = "ok"
WARNINGS = "warnings"
FAILED_RUN = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    uid TEXT PRIMARY KEY,
    trip_type TEXT,
    origin TEXT,
    destination TEXT,
    start_datetime TEXT,              -- local start time as trainlog shows it
    csv_row TEXT NOT NULL,            -- the trip's latest row from the trainlog CSV, as JSON
    fingerprint TEXT NOT NULL,        -- of the latest CSV row, see trip_sync.trip_fingerprint
    status TEXT NOT NULL,
    status_detail TEXT NOT NULL DEFAULT '',
    sent_fingerprint TEXT,            -- the version whose points are in Dawarich
    sent_start_timestamp INTEGER,     -- time range of those points
    sent_end_timestamp INTEGER,
    sent_point_count INTEGER,
    sent_at TEXT,
    phone_points_deleted INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,            -- nightly, manual or upload
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL,
    trips_sent INTEGER NOT NULL DEFAULT 0,
    trips_removed INTEGER NOT NULL DEFAULT 0,
    phone_points_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    level TEXT NOT NULL,              -- info, warning or error
    message TEXT NOT NULL,
    trip_uid TEXT,
    created_at TEXT NOT NULL
);
"""

_database_file = None


def init_database(database_file):
    global _database_file
    _database_file = database_file
    database_file.parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode = WAL")


@contextmanager
def connect():
    """A connection that commits when the block ends (or rolls back on an error)."""
    connection = sqlite3.connect(_database_file, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Trips ------------------------------------------------------------------------


def get_trip(uid):
    with connect() as connection:
        return connection.execute("SELECT * FROM trips WHERE uid = ?", (uid,)).fetchone()


def save_trip(row, fingerprint, status, status_detail=""):
    """Insert or update a trip's CSV data and status. What's recorded about its points
    in Dawarich (the sent_* columns) is left as it is."""
    now = now_iso()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO trips (uid, trip_type, origin, destination, start_datetime, csv_row, fingerprint,
                               status, status_detail, first_seen_at, updated_at)
            VALUES (:uid, :trip_type, :origin, :destination, :start_datetime, :csv_row, :fingerprint,
                    :status, :status_detail, :now, :now)
            ON CONFLICT (uid) DO UPDATE SET
                trip_type = :trip_type, origin = :origin, destination = :destination,
                start_datetime = :start_datetime, csv_row = :csv_row, fingerprint = :fingerprint,
                status = :status, status_detail = :status_detail, updated_at = :now
            """,
            {
                "uid": row["uid"],
                "trip_type": row.get("type", ""),
                "origin": row.get("origin_station", ""),
                "destination": row.get("destination_station", ""),
                "start_datetime": row.get("start_datetime", ""),
                "csv_row": json.dumps(row),
                "fingerprint": fingerprint,
                "status": status,
                "status_detail": status_detail,
                "now": now,
            },
        )


def set_trip_status(uid, status, status_detail=""):
    with connect() as connection:
        connection.execute(
            "UPDATE trips SET status = ?, status_detail = ?, updated_at = ? WHERE uid = ?",
            (status, status_detail, now_iso(), uid),
        )


def record_trip_sent(uid, fingerprint, start_timestamp, end_timestamp, point_count):
    with connect() as connection:
        connection.execute(
            """
            UPDATE trips SET sent_fingerprint = ?, sent_start_timestamp = ?, sent_end_timestamp = ?,
                             sent_point_count = ?, sent_at = ?
            WHERE uid = ?
            """,
            (fingerprint, start_timestamp, end_timestamp, point_count, now_iso(), uid),
        )


def record_trip_points_removed(uid):
    with connect() as connection:
        connection.execute(
            """
            UPDATE trips SET sent_fingerprint = NULL, sent_start_timestamp = NULL, sent_end_timestamp = NULL,
                             sent_point_count = NULL, sent_at = NULL
            WHERE uid = ?
            """,
            (uid,),
        )


def add_phone_points_deleted(uid, count):
    with connect() as connection:
        connection.execute(
            "UPDATE trips SET phone_points_deleted = phone_points_deleted + ? WHERE uid = ?", (count, uid)
        )


def trips_not_removed():
    with connect() as connection:
        return connection.execute("SELECT * FROM trips WHERE status != ?", (REMOVED,)).fetchall()


def trips_to_check_for_phone_points(since):
    """Trips in Dawarich that were sent, or took place, after `since` (a datetime)."""
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM trips WHERE status = ? AND (sent_at >= ? OR sent_end_timestamp >= ?)",
            (IN_DAWARICH, since.isoformat(timespec="seconds"), int(since.timestamp())),
        ).fetchall()


def all_trips(status=None):
    query = "SELECT * FROM trips"
    parameters = ()
    if status:
        query += " WHERE status = ?"
        parameters = (status,)
    query += " ORDER BY start_datetime DESC"
    with connect() as connection:
        return connection.execute(query, parameters).fetchall()


def trip_counts_by_status():
    with connect() as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS count FROM trips GROUP BY status").fetchall()
    return {row["status"]: row["count"] for row in rows}


# --- Runs -------------------------------------------------------------------------


def start_run(trigger):
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO runs (trigger, started_at, outcome) VALUES (?, ?, ?)", (trigger, now_iso(), RUNNING)
        )
        return cursor.lastrowid


def add_run_event(run_id, level, message, trip_uid=None):
    with connect() as connection:
        connection.execute(
            "INSERT INTO run_events (run_id, level, message, trip_uid, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, level, message, trip_uid, now_iso()),
        )


def finish_run(run_id, outcome, trips_sent, trips_removed, phone_points_deleted):
    with connect() as connection:
        connection.execute(
            """
            UPDATE runs SET finished_at = ?, outcome = ?, trips_sent = ?, trips_removed = ?,
                            phone_points_deleted = ?
            WHERE id = ?
            """,
            (now_iso(), outcome, trips_sent, trips_removed, phone_points_deleted, run_id),
        )


def mark_interrupted_runs_as_failed():
    """Runs still 'running' when the app starts were interrupted by a restart."""
    with connect() as connection:
        interrupted = connection.execute("SELECT id FROM runs WHERE outcome = ?", (RUNNING,)).fetchall()
        for run in interrupted:
            connection.execute(
                "UPDATE runs SET outcome = ?, finished_at = ? WHERE id = ?", (FAILED_RUN, now_iso(), run["id"])
            )
            connection.execute(
                "INSERT INTO run_events (run_id, level, message, created_at) VALUES (?, 'error', ?, ?)",
                (run["id"], "The run was interrupted because the app restarted.", now_iso()),
            )


def get_run(run_id):
    with connect() as connection:
        return connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def recent_runs(limit=30):
    with connect() as connection:
        return connection.execute(
            """
            SELECT runs.*,
                   (SELECT COUNT(*) FROM run_events
                    WHERE run_events.run_id = runs.id AND level IN ('warning', 'error')) AS problem_count
            FROM runs ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def run_events(run_id):
    with connect() as connection:
        return connection.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
