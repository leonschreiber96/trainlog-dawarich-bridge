"""Checks on the last day of phone points that catch tracking problems early.

Each check returns a list of warning messages (empty if all is well).
"""

from datetime import datetime

from .converter import distance_meters
from .phone_points import is_trip_point, tracker_id_for_trip

# Dawarich's default "minutes between routes": phone points closer in time than this can end up in one track.
TRACK_GAP_SECONDS = 30 * 60

# Trip points without a track this close to the tracked ones next to them are a stop that Dawarich
# leaves out of its tracks (see untracked_points_outside_stops). Dawarich's stop radius is at least
# 100 m; this leaves room for a larger visit radius in its settings.
STOP_RADIUS_METERS = 200


def looks_like_shortcut_point(point):
    """The iOS Shortcut sends battery and Wi-Fi but no accuracy; the Dawarich app sends accuracy."""
    return point.get("accuracy") is None and point.get("battery") is not None


def check_phone_tracker_ids(points):
    """The Shortcut's tid must match the Dawarich app's tracker id, otherwise Dawarich
    keeps their points in separate tracks (e.g. after reinstalling the app).

    Only the latest point of each is compared, so a fixed tid counts right away,
    even while older points of the day still have the old one."""
    phone_points = sorted((point for point in points if not is_trip_point(point)),
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


def trip_track_problem(trip_name, trip_uid, route, points_around_trip):
    """Did Dawarich make a track from the trip, and cut the phone's track where the trip is?
    None if so, otherwise (what's wrong, whether sending the trip's points again can fix it).

    If the trip has no track, nothing cuts the phone's track either: Dawarich only cuts a
    track where another tracker's track fills its gap. The phone's track then runs in a
    straight line from where the trip starts to where it ends.

    points_around_trip: Dawarich's points (with anomalies) from TRACK_GAP_SECONDS before
    the trip to TRACK_GAP_SECONDS after it."""
    if not any("track_id" in point for point in points_around_trip):
        return None  # this Dawarich doesn't say which track a point belongs to

    tracker_id = tracker_id_for_trip(trip_uid)
    trip_points = [point for point in points_around_trip if point.get("tracker_id") == tracker_id]
    if not trip_points:
        return None
    noise = [point for point in trip_points if point.get("anomaly")]
    untracked = untracked_points_outside_stops(trip_points)
    if len(noise) > len(trip_points) / 2:
        return (f"Dawarich flagged {len(noise)} of the {len(trip_points)} points of {trip_name} as GPS noise, "
                "so it isn't shown as a track.", False)
    if untracked:
        return (f"{len(untracked)} of the {len(trip_points)} points of {trip_name} belonged to no track in "
                "Dawarich, so the trip wasn't shown as a track, and the phone's track may have run in a "
                "straight line across it.", True)

    trip_start, trip_end = route[0]["timestamp"], route[-1]["timestamp"]
    phone_points = sorted((point for point in points_around_trip
                           if not is_trip_point(point) and not point.get("anomaly")),
                          key=lambda point: point["timestamp"])
    if any(trip_start <= point["timestamp"] <= trip_end for point in phone_points):
        return None  # phone points during the trip are deleted first; judged in a later run
    before = [point for point in phone_points if point["timestamp"] < trip_start]
    after = [point for point in phone_points if point["timestamp"] > trip_end]
    if before and after and before[-1].get("track_id") is not None \
            and before[-1].get("track_id") == after[0].get("track_id"):
        return (f"The phone's track ran in a straight line across {trip_name}: Dawarich didn't cut it where "
                "the trip is.", True)
    return None


def untracked_points_outside_stops(trip_points):
    """The trip's points (not GPS noise) that belong to no track, except where the ride stood still.

    Dawarich leaves the points of a stop out of its tracks on purpose: where points stay within its
    stop radius (100 m, or the visit radius in its settings if larger) for 5 minutes or more, the
    track ends and a new one starts when the ride leaves. That also happens to a trip's points, e.g.
    at a station where the train waits 10 minutes, or a bus leaving late. Nothing of the trip is
    missing from the map then, and neither sending the points again nor recalculating changes it.

    So a run of untracked points is left out if all of them lie within STOP_RADIUS_METERS of the
    tracked trip points next to it (on both sides, or the one side at the trip's start or end)."""
    points = sorted((point for point in trip_points if not point.get("anomaly")),
                    key=lambda point: point["timestamp"])
    untracked = []
    run_start = None
    for index, point in enumerate(points + [None]):
        if point is not None and point.get("track_id") is None:
            if run_start is None:
                run_start = index
            continue
        if run_start is not None:
            run = points[run_start:index]
            neighbours = [neighbour for neighbour in (points[run_start - 1] if run_start else None, point)
                          if neighbour is not None]
            if not neighbours or any(distance_meters(position(stopped), position(neighbour)) > STOP_RADIUS_METERS
                                     for stopped in run for neighbour in neighbours):
                untracked += run
            run_start = None
    return untracked


def position(point):
    return [float(point["longitude"]), float(point["latitude"])]


def format_local_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
