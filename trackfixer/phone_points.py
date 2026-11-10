"""Finding phone points (Dawarich app, iOS Shortcut) that were recorded during a trainlog trip.

trainlog routes follow the real tracks and roads, phone points cut corners. Phone
points recorded during a trip only create a second, worse track for the same ride,
so they get deleted.

Points here are dicts with "timestamp", "latitude" and "longitude", either from
Dawarich's API or from converter.convert_trip.
"""

import math

TRAINLOG_TRACKER_PREFIX = "trainlog-"

# trainlog times are only accurate to the minute, and rides can run late. Phone points
# up to this long before/after a trip are deleted too, but only if they lie on the route.
TIME_BUFFER_SECONDS = 3 * 60
MAX_DISTANCE_FROM_ROUTE_METERS = 50

# If most phone points recorded during a trip are this far from its route, the trip's
# time in trainlog is probably wrong (wrong day, time zone...). Nothing gets deleted then.
FAR_FROM_ROUTE_METERS = 1000
MIN_POINTS_TO_JUDGE_TRIP_TIME = 3

METERS_PER_DEGREE_LATITUDE = 111_320


def tracker_id_for_trip(trip_uid):
    return f"{TRAINLOG_TRACKER_PREFIX}{trip_uid}"


def is_trainlog_point(point):
    return (point.get("tracker_id") or "").startswith(TRAINLOG_TRACKER_PREFIX)


def phone_points_during_trip(route, points_around_trip):
    """Phone points recorded while riding: all of them inside the trip's time, plus
    those just before or after it that lie on the route."""
    trip_start = route[0]["timestamp"]
    trip_end = route[-1]["timestamp"]

    found = []
    for point in points_around_trip:
        if is_trainlog_point(point):
            continue
        inside_trip_time = trip_start <= point["timestamp"] <= trip_end
        inside_buffer_time = trip_start - TIME_BUFFER_SECONDS <= point["timestamp"] <= trip_end + TIME_BUFFER_SECONDS
        if inside_trip_time:
            found.append(point)
        elif inside_buffer_time and distance_to_route_meters(point, route) <= MAX_DISTANCE_FROM_ROUTE_METERS:
            found.append(point)
    return found


def trip_time_looks_wrong(route, phone_points):
    """True if most phone points recorded during the trip were nowhere near its route."""
    trip_start = route[0]["timestamp"]
    trip_end = route[-1]["timestamp"]
    inside_trip_time = [point for point in phone_points if trip_start <= point["timestamp"] <= trip_end]
    far_away = [point for point in inside_trip_time if distance_to_route_meters(point, route) > FAR_FROM_ROUTE_METERS]
    return len(far_away) >= MIN_POINTS_TO_JUDGE_TRIP_TIME and len(far_away) > len(inside_trip_time) / 2


def distance_to_route_meters(point, route):
    """Shortest distance from a point to the line connecting the route's points."""
    route_segments = list(zip(route, route[1:])) or [(route[0], route[0])]
    return min(distance_to_segment_meters(point, start, end) for start, end in route_segments)


def distance_to_segment_meters(point, segment_start, segment_end):
    # At these distances the earth is flat enough: turn lat/lon into x/y meters around the point.
    point_latitude = float(point["latitude"])
    point_longitude = float(point["longitude"])
    meters_per_degree_longitude = METERS_PER_DEGREE_LATITUDE * math.cos(math.radians(point_latitude))

    def to_meters(other):
        x = (float(other["longitude"]) - point_longitude) * meters_per_degree_longitude
        y = (float(other["latitude"]) - point_latitude) * METERS_PER_DEGREE_LATITUDE
        return x, y

    # The point itself is now at (0, 0).
    start_x, start_y = to_meters(segment_start)
    end_x, end_y = to_meters(segment_end)
    segment_x, segment_y = end_x - start_x, end_y - start_y
    segment_length_squared = segment_x**2 + segment_y**2

    if segment_length_squared == 0:
        return math.hypot(start_x, start_y)

    # How far along the segment (0 = start, 1 = end) the point is closest to it.
    position_on_segment = -(start_x * segment_x + start_y * segment_y) / segment_length_squared
    position_on_segment = max(0.0, min(1.0, position_on_segment))

    closest_x = start_x + position_on_segment * segment_x
    closest_y = start_y + position_on_segment * segment_y
    return math.hypot(closest_x, closest_y)
