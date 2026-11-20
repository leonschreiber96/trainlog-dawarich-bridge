"""Checks on the last day of phone points that catch tracking problems early.

Each check returns a list of warning messages (empty if all is well).
"""

from datetime import datetime

from .phone_points import is_trainlog_point, tracker_id_for_trip

# Dawarich's default "minutes between routes": phone points closer in time than this can end up in one track.
TRACK_GAP_SECONDS = 30 * 60

RECALCULATE_HINT = 'Use "Recalculate tracks & stats" in Dawarich to rebuild them.'


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


def check_trip_tracks(trip_name, trip_uid, route, points_around_trip):
    """Did Dawarich make a track from the trip, and cut the phone's track where the trip is?

    If the trip has no track, nothing cuts the phone's track either: Dawarich only cuts a
    track where another tracker's track fills its gap. The phone's track then runs in a
    straight line from where the trip starts to where it ends.

    points_around_trip: Dawarich's points (with anomalies) from TRACK_GAP_SECONDS before
    the trip to TRACK_GAP_SECONDS after it."""
    if not any("track_id" in point for point in points_around_trip):
        return []  # this Dawarich doesn't say which track a point belongs to

    tracker_id = tracker_id_for_trip(trip_uid)
    trip_points = [point for point in points_around_trip if point.get("tracker_id") == tracker_id]
    if not trip_points:
        return []
    noise = [point for point in trip_points if point.get("anomaly")]
    untracked = [point for point in trip_points if not point.get("anomaly") and point.get("track_id") is None]
    if len(noise) > len(trip_points) / 2:
        return [f"Dawarich flagged {len(noise)} of the {len(trip_points)} points of {trip_name} as GPS noise, "
                "so it isn't shown as a track."]
    if untracked:
        return [f"{len(untracked)} of the {len(trip_points)} points of {trip_name} belong to no track in Dawarich, "
                "so the trip isn't shown as a track, and the phone's track may run in a straight line across it. "
                + RECALCULATE_HINT]

    trip_start, trip_end = route[0]["timestamp"], route[-1]["timestamp"]
    phone_points = sorted((point for point in points_around_trip
                           if not is_trainlog_point(point) and not point.get("anomaly")),
                          key=lambda point: point["timestamp"])
    if any(trip_start <= point["timestamp"] <= trip_end for point in phone_points):
        return []  # phone points during the trip are deleted first; judged in a later run
    before = [point for point in phone_points if point["timestamp"] < trip_start]
    after = [point for point in phone_points if point["timestamp"] > trip_end]
    if before and after and before[-1].get("track_id") is not None \
            and before[-1].get("track_id") == after[0].get("track_id"):
        return [f"The phone's track runs in a straight line across {trip_name}: Dawarich didn't cut it where "
                "the trip is. " + RECALCULATE_HINT]
    return []


def format_local_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
