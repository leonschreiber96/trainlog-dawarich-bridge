"""Deleting the phone points recorded during trainlog trips, with a backup of everything deleted."""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import database
from .converter import convert_trip
from .phone_points import TIME_BUFFER_SECONDS, phone_points_during_trip, trip_time_looks_wrong
from .trip_sync import describe_stored_trip

# Trips sent to Dawarich, or that took place, within this many days are checked every run.
# That catches phone points that reach Dawarich late, e.g. after the phone was offline on a train.
RECHECK_DAYS = 30

# Points of trips imported before this app existed. They duplicate the trips this app sends.
OLD_TRAINLOG_IMPORT_TRACKER_ID = "trainlog-csv"


def remove_phone_points_during_trips(dawarich, settings, log):
    since = datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)
    trips = database.trips_to_check_for_phone_points(since)

    points_to_delete = {}  # point id -> point
    trip_uid_of_point = {}  # point id -> trip uid
    trips_with_old_trainlog_imports = []

    for trip in trips:
        route, _ = convert_trip(json.loads(trip["csv_row"]))
        points_around_trip = dawarich.get_points(
            route[0]["timestamp"] - TIME_BUFFER_SECONDS, route[-1]["timestamp"] + TIME_BUFFER_SECONDS
        )
        if any(point.get("tracker_id") == OLD_TRAINLOG_IMPORT_TRACKER_ID for point in points_around_trip):
            trips_with_old_trainlog_imports.append(trip)

        found = phone_points_during_trip(route, points_around_trip)
        if not found:
            continue
        if trip_time_looks_wrong(route, found):
            log.warning(
                f"Most phone points recorded during {describe_stored_trip(trip)} were more than 1 km from its "
                "route, so its time in trainlog is probably wrong. No phone points were deleted for it.",
                trip["uid"],
            )
            continue
        for point in found:
            points_to_delete[point["id"]] = point
            trip_uid_of_point[point["id"]] = trip["uid"]

    if trips_with_old_trainlog_imports:
        log.warning(
            f"Points from old trainlog imports (tracker id {OLD_TRAINLOG_IMPORT_TRACKER_ID}) are still in Dawarich "
            f"around {len(trips_with_old_trainlog_imports)} trip(s), e.g. "
            f"{describe_stored_trip(trips_with_old_trainlog_imports[0])}. They duplicate the trips this app "
            "sends: delete the old trainlog imports on Dawarich's Imports page."
        )

    if not points_to_delete:
        return
    if len(points_to_delete) > settings.max_phone_points_deleted_per_run:
        log.error(
            f"{len(points_to_delete)} phone points would be deleted, more than the limit of "
            f"{settings.max_phone_points_deleted_per_run} per run, so nothing was deleted. If that's expected "
            "(e.g. on the first run over your whole history), raise MAX_PHONE_POINTS_DELETED_PER_RUN for one run."
        )
        return

    backup_file = back_up_points(list(points_to_delete.values()), settings, log.run_id)
    dawarich.delete_points(list(points_to_delete))

    deleted_per_trip = Counter(trip_uid_of_point.values())
    for trip_uid, count in deleted_per_trip.items():
        database.add_phone_points_deleted(trip_uid, count)
    log.phone_points_deleted = len(points_to_delete)
    log.info(
        f"Deleted {len(points_to_delete)} phone points recorded during {len(deleted_per_trip)} trips. "
        f"Backup: {backup_file}"
    )


def back_up_points(points, settings, run_id):
    settings.deleted_points_directory.mkdir(parents=True, exist_ok=True)
    backup_file = settings.deleted_points_directory / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_run-{run_id}.json"
    backup_file.write_text(json.dumps(points, indent=2))
    return backup_file
