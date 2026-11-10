"""Checks on the last day of phone points that catch tracking problems early.

Each check returns a list of warning messages (empty if all is well).
"""

from collections import Counter
from datetime import datetime

from .phone_points import is_trainlog_point


def looks_like_shortcut_point(point):
    """The iOS Shortcut sends battery and Wi-Fi but no accuracy; the Dawarich app sends accuracy."""
    return point.get("accuracy") is None and point.get("battery") is not None


def check_phone_tracker_ids(points):
    """The Shortcut's tid must match the Dawarich app's tracker id, otherwise Dawarich
    keeps their points in separate tracks (e.g. after reinstalling the app)."""
    phone_points = [point for point in points if not is_trainlog_point(point)]
    counts = Counter()
    for point in phone_points:
        source = "Shortcut" if looks_like_shortcut_point(point) else "app"
        counts[(point.get("tracker_id") or "", source)] += 1

    tracker_ids = {tracker_id for tracker_id, _ in counts}
    if len(tracker_ids) <= 1 and "" not in tracker_ids:
        return []

    details = ", ".join(
        f"{tracker_id or '(empty)'}: {count} {source} points" for (tracker_id, source), count in sorted(counts.items())
    )
    return [
        "Phone points of the last 24 hours have different or empty tracker ids, so Dawarich "
        f"won't combine them into one track: {details}. Set the Shortcut's tid to the app's tracker id."
    ]


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
