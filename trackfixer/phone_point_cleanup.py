"""Deleting the phone points recorded during trainlog trips, with a backup of everything deleted."""

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import database
from .converter import convert_trip
from .health_checks import TRACK_GAP_SECONDS, trip_track_problem
from .phone_points import phone_points_during_trip, trip_time_looks_wrong
from .trip_sync import describe_stored_trip, resend_trip

# Trips sent to Dawarich, or that took place, within this many days are checked every run.
# That catches phone points that reach Dawarich late, e.g. after the phone was offline on a train.
# Whether Dawarich made proper tracks from them is checked too, once it had time for that.
RECHECK_DAYS = 30
TIME_FOR_DAWARICH_TO_BUILD_TRACKS = timedelta(hours=1)

# Dawarich builds tracks from points up to 6 hours old as they come in (Tracks::IncrementalGenerator),
# and from older ones in a separate backfill. Trips sent by the nightly run, mostly over 6 hours after
# the ride, have got their tracks; the first one seen without was sent by a run during the same day.
# So in case that's what went wrong, a trip is only sent again once its points go through the backfill.
DAWARICH_REALTIME_WINDOW_SECONDS = 6 * 60 * 60

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
            route[0]["timestamp"] - TRACK_GAP_SECONDS, route[-1]["timestamp"] + TRACK_GAP_SECONDS
        )
        if any(point.get("tracker_id") == OLD_TRAINLOG_IMPORT_TRACKER_ID for point in points_around_trip):
            trips_with_old_trainlog_imports.append(trip)

        found = phone_points_during_trip(route, points_around_trip)
        if not found:
            repair_trip_track(trip, route, points_around_trip, dawarich, log)
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
    deleted_per_trip = Counter(trip_uid_of_point.values())
    log.info(
        f"Deleting {len(points_to_delete)} phone points recorded during {len(deleted_per_trip)} trips. "
        f"Backup: {backup_file}"
    )
    dawarich.delete_points(list(points_to_delete))

    for trip_uid, count in deleted_per_trip.items():
        database.add_phone_points_deleted(trip_uid, count)
    log.phone_points_deleted = len(points_to_delete)


def repair_trip_track(trip, route, points_around_trip, dawarich, log):
    """If Dawarich made no proper track from a trip, send its points again, once.

    "Recalculate tracks & stats" in Dawarich would fix it too, but it deletes and rebuilds
    all tracks of a year, and with them every manual transport mode correction."""
    if not sent_long_enough_ago(trip):
        return
    problem = trip_track_problem(describe_stored_trip(trip), trip["uid"], route, points_around_trip)
    if problem is None:
        return
    description, resending_can_fix_it = problem
    if not resending_can_fix_it:
        log.warning(description, trip["uid"])
    elif trip["resent_for_track_at"]:
        log.warning(f"{description} Sending its points again didn't help. \"Recalculate tracks & stats\" in "
                    "Dawarich fixes it, but also deletes the manual transport mode corrections of that year.",
                    trip["uid"])
    elif time.time() - route[-1]["timestamp"] >= DAWARICH_REALTIME_WINDOW_SECONDS:
        resend_trip(trip, route, dawarich)
        database.record_trip_resent_for_track(trip["uid"])
        log.info(f"{description} Its points were sent again, so Dawarich builds its track.", trip["uid"])


def sent_long_enough_ago(trip):
    """Dawarich builds tracks from points sent hours after they were recorded with a delay
    (a minute, then a few more for cutting the phone's track), so fresh trips aren't judged yet."""
    if not trip["sent_at"]:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(trip["sent_at"]) >= TIME_FOR_DAWARICH_TO_BUILD_TRACKS


def back_up_points(points, settings, run_id):
    settings.deleted_points_directory.mkdir(parents=True, exist_ok=True)
    backup_file = settings.deleted_points_directory / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_run-{run_id}.json"
    backup_file.write_text(json.dumps(points, indent=2))
    return backup_file
