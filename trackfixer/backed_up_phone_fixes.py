"""One-off: keep the phone points in the backups of deleted points as phone fixes of their trips.

Phone points the phone reports as inaccurate can time a trip now (see phone_anchors.py). Before, only
the accurate ones were kept as phone fixes; the others were deleted from Dawarich, so they're only in
deleted-points/. This keeps those recorded during each trip in Dawarich as its phone fixes. A trip that
gets new ones gets a new fingerprint, so the next run converts it and sends it again. Running it again
is harmless: fixes already kept are skipped.

    python -m trackfixer phone-fixes-from-backups            # dry run: shows what would be added
    python -m trackfixer phone-fixes-from-backups --apply
"""

import bisect
import json

from . import database, phone_anchors
from .converter import trip_time_range
from .trip_sync import describe_stored_trip


def phone_fixes_from_backups(settings, apply):
    points = backed_up_points(settings)
    timestamps = [point["timestamp"] for point in points]
    trips_with_new_fixes = total = 0

    for trip in database.all_trips(database.IN_DAWARICH):
        row = json.loads(trip["csv_row"])
        if row.get("type") in phone_anchors.TRIP_TYPES_WITHOUT_PHONE_ANCHORS:
            continue
        try:
            start, end = trip_time_range(row)
        except ValueError:
            continue
        during_trip = points[bisect.bisect_left(timestamps, start):bisect.bisect_right(timestamps, end)]
        kept = {fix["timestamp"] for fix in database.phone_fixes(trip["uid"])}
        new_fixes = [fix for fix in phone_anchors.fixes_from_points(during_trip, start, end)
                     if fix["timestamp"] not in kept]
        if not new_fixes:
            continue

        trips_with_new_fixes += 1
        total += len(new_fixes)
        print(f"{describe_stored_trip(trip)}: {len(new_fixes)} phone points")
        if apply:
            database.add_phone_fixes(trip["uid"], new_fixes)

    if not apply:
        print(f"Dry run: {total} phone points would be kept for {trips_with_new_fixes} trips. Add --apply to do it.")
    elif total:
        print(f"Done: {total} phone points kept for {trips_with_new_fixes} trips. The next run sends those trips again.")
    else:
        print("Nothing to add.")


def backed_up_points(settings):
    """All points in deleted-points/, oldest first, each once."""
    by_id = {}
    for backup_file in sorted(settings.deleted_points_directory.glob("*.json")):
        try:
            points = json.loads(backup_file.read_text())
        except (OSError, ValueError) as error:
            print(f"Skipped {backup_file.name}: {error}")
            continue
        for point in points:
            if isinstance(point, dict) and isinstance(point.get("timestamp"), (int, float)):
                by_id[point.get("id", (point["timestamp"], point.get("latitude"), point.get("longitude")))] = point
    return sorted(by_id.values(), key=lambda point: point["timestamp"])
