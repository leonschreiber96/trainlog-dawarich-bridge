"""One-off repair: give old iOS Shortcut points the Dawarich app's tracker id.

Before the Shortcut's tid was set, its points had no tracker id. Dawarich builds tracks
per tracker id, so those points formed their own tracks (straight lines between two
Shortcut points) next to the app's track of the same walk.

Dawarich's API can't change a point's tracker id, so each such point is sent again with
the app's tracker id, one second later (Dawarich refuses a second point at the same place
and time), and only then is the original deleted. Every original is backed up first.
Running it again is harmless: copies that already exist are skipped by Dawarich.

    python -m trackfixer fix-shortcut-tracker-ids --tracker-id <app's id> --since 2025-01-01
    python -m trackfixer fix-shortcut-tracker-ids --tracker-id <app's id> --since 2025-01-01 --apply
"""

import json
from datetime import datetime

from .health_checks import looks_like_shortcut_point


def fix_shortcut_tracker_ids(dawarich, settings, app_tracker_id, since, apply):
    total = 0
    for month_start, month_end in calendar_months(since, datetime.now()):
        points = dawarich.get_points(month_start.timestamp(), month_end.timestamp() - 1)
        shortcut_points_without_tracker_id = [
            point for point in points if not point.get("tracker_id") and looks_like_shortcut_point(point)
        ]
        if not shortcut_points_without_tracker_id:
            continue

        total += len(shortcut_points_without_tracker_id)
        print(f"{month_start:%Y-%m}: {len(shortcut_points_without_tracker_id)} Shortcut points without tracker id")
        if apply:
            move_to_tracker(dawarich, shortcut_points_without_tracker_id, app_tracker_id, settings, month_start)

    if not apply:
        print(f"Dry run: {total} points would get the tracker id {app_tracker_id}. Add --apply to do it.")
    elif total:
        print(f"Done: {total} points now have the tracker id {app_tracker_id}. In Dawarich, press "
              "'Recalculate tracks & stats' so the tracks of those days are rebuilt with them.")
    else:
        print("Nothing to fix.")


def move_to_tracker(dawarich, points, tracker_id, settings, month_start):
    settings.deleted_points_directory.mkdir(parents=True, exist_ok=True)
    backup_file = settings.deleted_points_directory / f"shortcut-points-without-tracker-id_{month_start:%Y-%m}.json"
    backup_file.write_text(json.dumps(points, indent=2))

    copies = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(point["longitude"]), float(point["latitude"])]},
            "properties": {
                "timestamp": point["timestamp"] + 1,
                "device_id": tracker_id,
                # Dawarich stores battery_level × 100 as a whole number; + 0.5 avoids 0.29 → 28.99 → 28.
                "battery_level": (point["battery"] + 0.5) / 100 if point.get("battery") else None,
                "wifi": point.get("ssid"),
            },
        }
        for point in points
    ]
    dawarich.create_raw_points(copies)
    dawarich.delete_points([point["id"] for point in points])


def calendar_months(start, end):
    """(first moment of the month, first moment of the next month) from start's month to end's month."""
    month_start = datetime(start.year, start.month, 1)
    while month_start <= end:
        next_month = datetime(month_start.year + month_start.month // 12, month_start.month % 12 + 1, 1)
        yield month_start, next_month
        month_start = next_month
