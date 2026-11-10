"""Keeping Dawarich in sync with the trips in trainlog.

Each trip's points go to Dawarich as their own tracker, "trainlog-<trip uid>", so
Dawarich makes a separate track per trip, even for tight connections. No Dawarich
imports are used: a trip whose route or times change in trainlog gets its points
deleted and sent again, without leaving empty imports behind.
"""

import hashlib
import json
import time

from . import database
from .converter import convert_trip, parse_utc_timestamp
from .phone_points import tracker_id_for_trip

NOT_A_JOURNEY_TYPES = {"accommodation", "restaurant", "poi"}

# Only these columns change a trip's points. Editing e.g. a note in trainlog changes nothing.
FINGERPRINT_COLUMNS = ("path", "utc_start_datetime", "utc_end_datetime", "departure_delay", "arrival_delay", "waypoints")

# Increase this after changing converter.py, so all trips get converted and sent again.
CONVERTER_VERSION = 1

# More trips than this missing from one export at once looks like an incomplete download.
MAX_TRIPS_REMOVED_PER_RUN = 10


def sync_trips(rows, is_complete_export, dawarich, log):
    """Send new and changed trips to Dawarich. If the CSV holds all trips, also remove
    trips from Dawarich that were deleted in trainlog."""
    trip_uids_in_export = set()
    trips_sent_before = log.trips_sent
    for row in rows:
        row["uid"] = (row.get("uid") or "").strip()
        if not row["uid"]:
            log.warning("A row of the trainlog CSV has no uid and was skipped.")
            continue
        trip_uids_in_export.add(row["uid"])
        sync_trip(row, dawarich, log)

    log.info(f"The trainlog CSV has {len(trip_uids_in_export)} trips; "
             f"{log.trips_sent - trips_sent_before} were new or changed and sent to Dawarich.")
    if is_complete_export:
        remove_trips_deleted_in_trainlog(trip_uids_in_export, dawarich, log)


def sync_trip(row, dawarich, log):
    uid = row["uid"]
    fingerprint = trip_fingerprint(row)
    stored_trip = database.get_trip(uid)
    if is_up_to_date(stored_trip, fingerprint):
        return

    if stored_trip is not None and stored_trip["sent_fingerprint"]:
        remove_trip_points(stored_trip, dawarich)
        log.info(f"{describe_csv_row(row)} changed in trainlog; its points are replaced.", uid)

    status, reason = reason_not_to_send(row)
    if status:
        database.save_trip(row, fingerprint, status, reason)
        return

    try:
        route, converter_warnings = convert_trip(row)
    except ValueError as error:
        database.save_trip(row, fingerprint, database.FAILED, str(error))
        log.warning(f"Couldn't convert {describe_csv_row(row)}: {error}", uid)
        return

    dawarich.create_points(route, tracker_id_for_trip(uid))
    database.save_trip(row, fingerprint, database.IN_DAWARICH, "; ".join(converter_warnings))
    database.record_trip_sent(uid, fingerprint, route[0]["timestamp"], route[-1]["timestamp"], len(route))
    log.trips_sent += 1


def is_up_to_date(stored_trip, fingerprint):
    if stored_trip is None:
        return False
    if stored_trip["status"] == database.IN_DAWARICH:
        return stored_trip["sent_fingerprint"] == fingerprint
    if stored_trip["status"] in (database.SKIPPED, database.FAILED):
        return stored_trip["fingerprint"] == fingerprint
    return False  # planned trips are checked every run, removed ones may be back


def reason_not_to_send(row):
    """(status, reason) if the trip shouldn't go to Dawarich (yet), otherwise (None, "")."""
    trip_type = row.get("type", "")
    if trip_type in NOT_A_JOURNEY_TYPES:
        return database.SKIPPED, f"A {trip_type} is a place, not a journey."
    try:
        parse_utc_timestamp(row.get("utc_start_datetime"), "utc_start_datetime")
        trip_end = parse_utc_timestamp(row.get("utc_end_datetime"), "utc_end_datetime")
    except ValueError:
        return database.SKIPPED, "No exact date and time in trainlog."
    if trip_end > time.time():
        return database.PLANNED, "Will be sent once the trip is over."
    return None, ""


def remove_trips_deleted_in_trainlog(trip_uids_in_export, dawarich, log):
    deleted_trips = [trip for trip in database.trips_not_removed() if trip["uid"] not in trip_uids_in_export]
    if not deleted_trips:
        return
    if len(deleted_trips) > MAX_TRIPS_REMOVED_PER_RUN:
        log.warning(
            f"{len(deleted_trips)} known trips are missing from the trainlog export. That's more than "
            f"{MAX_TRIPS_REMOVED_PER_RUN} at once, so none were removed from Dawarich, in case the export "
            "was incomplete. If you really deleted them in trainlog, raise MAX_TRIPS_REMOVED_PER_RUN "
            "in trackfixer/trip_sync.py for one run."
        )
        return

    for trip in deleted_trips:
        if trip["sent_fingerprint"]:
            remove_trip_points(trip, dawarich)
        database.set_trip_status(trip["uid"], database.REMOVED, "Deleted in trainlog.")
        log.trips_removed += 1
        log.info(f"{describe_stored_trip(trip)} was deleted in trainlog; its points were removed.", trip["uid"])


def remove_trip_points(trip, dawarich):
    """Delete the points this app sent for a trip."""
    tracker_id = tracker_id_for_trip(trip["uid"])
    points = dawarich.get_points(trip["sent_start_timestamp"], trip["sent_end_timestamp"])
    dawarich.delete_points([point["id"] for point in points if point.get("tracker_id") == tracker_id])
    database.record_trip_points_removed(trip["uid"])


def trip_fingerprint(row):
    relevant = {column: row.get(column, "") for column in FINGERPRINT_COLUMNS}
    relevant["converter_version"] = CONVERTER_VERSION
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


def describe_csv_row(row):
    return describe_trip(row.get("start_datetime"), row.get("origin_station"), row.get("destination_station"))


def describe_stored_trip(trip):
    return describe_trip(trip["start_datetime"], trip["origin"], trip["destination"])


def describe_trip(start_datetime, origin, destination):
    """e.g. "the trip 2026-11-10 12:16 Elm Street → Harbour Gate"."""
    date = str(start_datetime or "")[:16] if len(str(start_datetime or "")) > 2 else "(no date)"
    return f"the trip {date} {origin} → {destination}"
