"""Checks on the last day of phone points that catch tracking problems early.

Each check returns a list of warning messages (empty if all is well).
"""

from datetime import datetime

from .phone_points import is_trainlog_point


def looks_like_shortcut_point(point):
    """The iOS Shortcut sends battery and Wi-Fi but no accuracy; the Dawarich app sends accuracy."""
    return point.get("accuracy") is None and point.get("battery") is not None


def check_phone_tracker_ids(points):
    """The Shortcut's tid must match the Dawarich app's tracker id, otherwise Dawarich
    keeps their points in separate tracks (e.g. after reinstalling the app).

    Only the latest point of each is compared, so a fixed tid counts right away,
    even while older points of the day still have the old one."""
    phone_points = sorted((point for point in points if not is_trainlog_point(point)),
                          key=lambda point: point["timestamp"])
    shortcut_points = [point for point in phone_points if looks_like_shortcut_point(point)]
    app_points = [point for point in phone_points if not looks_like_shortcut_point(point)]
    if not shortcut_points:
        return []  # check_shortcut_heartbeat reports that

    shortcut_tracker_id = shortcut_points[-1].get("tracker_id") or ""
    if not shortcut_tracker_id:
        return ["The iOS Shortcut sends points without a tracker id, so Dawarich won't combine them with the "
                "app's points into one track. Set the Shortcut's tid to the app's tracker id."]

    if app_points:
        app_tracker_id = app_points[-1].get("tracker_id") or ""
        if app_tracker_id != shortcut_tracker_id:
            return [f"The iOS Shortcut sends tracker id {shortcut_tracker_id}, but the Dawarich app now uses "
                    f"{app_tracker_id or '(none)'}, so Dawarich keeps their points in separate tracks. "
                    "Set the Shortcut's tid to the app's tracker id."]
    return []


def check_shortcut_heartbeat(points, start_timestamp, end_timestamp, max_silence_minutes):
    """The Shortcut should send a point every 15 minutes. A long silence means it stopped running."""
    shortcut_times = sorted(point["timestamp"] for point in points if looks_like_shortcut_point(point))
    if not shortcut_times:
        return ["The iOS Shortcut sent no points in the last 24 hours."]

    times = [start_timestamp] + shortcut_times + [end_timestamp]
    longest_silence_start, longest_silence_end = max(zip(times, times[1:]), key=lambda pair: pair[1] - pair[0])
    longest_silence_minutes = (longest_silence_end - longest_silence_start) / 60
    if longest_silence_minutes <= max_silence_minutes:
        return []

    return [
        f"The iOS Shortcut sent no points for {longest_silence_minutes:.0f} minutes "
        f"({format_local_time(longest_silence_start)} to {format_local_time(longest_silence_end)})."
    ]


def format_local_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
