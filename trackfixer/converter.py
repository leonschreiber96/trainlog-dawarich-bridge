"""Turn one trainlog trip (a row of trainlog's CSV export) into timed route points.

Ported from csv_to_dawarich.py with the same default settings and the same results.

How the timestamps are made:
* The trip starts at utc_start_datetime + departure_delay and ends at
  utc_end_datetime + arrival_delay (delays are in seconds).
* Stops along the way (trainlog "waypoints") with a known time become extra time
  anchors. Realtime stop times are used as they are; scheduled stop times get the
  delay, interpolated between the departure and arrival delay.
* trainlog times are only minute-precise, so an anchor may move forward by up to
  59 seconds to keep all anchor times strictly increasing.
* Two stops can even share a minute. So a section between two anchors may not be
  faster than twice the trip's average speed; if the minute windows don't allow that,
  the fastest section is made as slow as they allow.
* Between two anchors, time is spread over the route by distance.
* Finally the route is simplified: a vertex is only kept if it is needed to stay
  within 25 m of the real route, if it is an anchor, or as a checkpoint roughly
  every 120 seconds. No two points share a second.
"""

import bisect
import json
import math
from datetime import datetime, timezone

ENCODED_PATH_PRECISION = 5
ANCHOR_TIME_WINDOW_SECONDS = 59
MAX_SECTION_SPEED_FACTOR = 2
SIMPLIFY_TOLERANCE_METERS = 25
CHECKPOINT_INTERVAL_SECONDS = 120
MAX_STOP_DISTANCE_FROM_ROUTE_METERS = 500
EARTH_RADIUS_METERS = 6_371_000


def convert_trip(row):
    """Returns (route points, warnings). Raises ValueError if the trip can't be converted.

    Each route point is a dict with "timestamp", "latitude" and "longitude".
    """
    warnings = []
    route = decode_path(row.get("path"))

    scheduled_start = parse_utc_timestamp(row.get("utc_start_datetime"), "utc_start_datetime")
    scheduled_end = parse_utc_timestamp(row.get("utc_end_datetime"), "utc_end_datetime")
    if scheduled_end <= scheduled_start:
        raise ValueError("Scheduled end must be after scheduled start; no usable duration")

    departure_delay = parse_delay_seconds(row.get("departure_delay"))
    arrival_delay = parse_delay_seconds(row.get("arrival_delay"))
    actual_start = scheduled_start + departure_delay
    actual_end = scheduled_end + arrival_delay
    if actual_end <= actual_start:
        raise ValueError("Delay-adjusted end is not after start; check the delays")

    distance_along_route = [0.0]
    for vertex, next_vertex in zip(route, route[1:]):
        distance_along_route.append(distance_along_route[-1] + distance_meters(vertex, next_vertex))

    # An anchor is (index of a route vertex, the time the train was there).
    anchors = [(0, actual_start)]
    anchors += stop_anchors(row, route, scheduled_start, scheduled_end, departure_delay, arrival_delay, warnings)
    anchors.append((len(route) - 1, actual_end))

    average_speed = distance_along_route[-1] / (actual_end - actual_start)
    anchor_times = fit_anchor_times(anchors, distance_along_route, average_speed)
    if anchor_times is None:
        warnings.append("Inconsistent waypoint timing; using delay-adjusted trip endpoints only")
        anchors = [anchors[0], anchors[-1]]
        anchor_times = fit_anchor_times(anchors, distance_along_route, average_speed)
    if anchor_times is None:
        raise ValueError("Cannot reconcile endpoint times")

    if anchor_times[0] != round(actual_start) or anchor_times[-1] != round(actual_end):
        warnings.append(
            "Endpoint adjusted inside minute window: departure %+ds, arrival %+ds"
            % (anchor_times[0] - round(actual_start), anchor_times[-1] - round(actual_end))
        )

    anchor_indices = [index for index, _ in anchors]
    times = interpolate_times(route, distance_along_route, anchor_indices, anchor_times)
    kept_indices = simplify_route(route, times, anchor_indices)
    kept_indices = drop_points_sharing_a_second(kept_indices, times, anchor_indices)

    route_points = [
        {"timestamp": times[index], "latitude": route[index][1], "longitude": route[index][0]}
        for index in kept_indices
    ]
    return route_points, warnings


# --- Reading the CSV fields ---------------------------------------------------------


def parse_utc_timestamp(value, field_name):
    text = str(value or "").strip()
    if not text:
        raise ValueError("Missing " + field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid %s: %r" % (field_name, text)) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_delay_seconds(value):
    text = str(value or "").strip()
    if text.lower() in ("", "null", "none", "nan"):
        return 0.0
    seconds = float(text)
    if not math.isfinite(seconds):
        raise ValueError("Non-finite delay")
    return seconds


def decode_path(encoded_path):
    """Decode a Google encoded polyline into a list of [longitude, latitude] vertices."""
    text = str(encoded_path or "").strip()
    if not text:
        raise ValueError("Missing path")

    position = 0

    def read_next_number():
        nonlocal position
        number = shift = 0
        while True:
            if position >= len(text):
                raise ValueError("Truncated encoded path")
            chunk = ord(text[position]) - 63
            position += 1
            if not 0 <= chunk <= 63 or shift > 35:
                raise ValueError("Invalid encoded path")
            number |= (chunk & 31) << shift
            shift += 5
            if chunk < 32:
                return ~(number >> 1) if number & 1 else number >> 1

    latitude = longitude = 0
    vertices = []
    while position < len(text):
        latitude += read_next_number()
        longitude += read_next_number()
        vertex = [longitude / 10**ENCODED_PATH_PRECISION, latitude / 10**ENCODED_PATH_PRECISION]
        if not (-180 <= vertex[0] <= 180 and -90 <= vertex[1] <= 90):
            raise ValueError("Decoded coordinates out of range")
        if not vertices or vertex != vertices[-1]:
            vertices.append(vertex)

    if len(vertices) < 2:
        raise ValueError("Path has fewer than two distinct consecutive vertices")
    return vertices


def parse_waypoints(value):
    """trainlog's waypoints column: a JSON list, sometimes JSON-encoded several times."""
    for _ in range(6):
        if value is None or isinstance(value, list):
            return value or []
        if isinstance(value, str):
            value = value.strip()
            if value.lower() in ("", "null", "none", "nan"):
                return []
            value = json.loads(value)
        else:
            raise ValueError("waypoints must decode to a list or null")
    raise ValueError("Too many nested JSON encodings in waypoints")


# --- Timing -------------------------------------------------------------------------


def stop_anchors(row, route, scheduled_start, scheduled_end, departure_delay, arrival_delay, warnings):
    """Time anchors from the stops along the trip, in route order."""
    anchors = []
    previous_index = 0
    for waypoint in parse_waypoints(row.get("waypoints")):
        if not isinstance(waypoint, dict):
            raise ValueError("Waypoint must be an object")
        stop = waypoint.get("stop") or {}
        if not isinstance(stop, dict):
            raise ValueError("Waypoint stop must be an object")

        stop_time = time_at_stop(stop, scheduled_start, scheduled_end, departure_delay, arrival_delay)
        latitude = waypoint.get("lat", stop.get("lat"))
        longitude = waypoint.get("lng", stop.get("lng"))
        if stop_time is None or latitude is None or longitude is None:
            continue

        stop_position = [float(longitude), float(latitude)]
        if not all(math.isfinite(coordinate) for coordinate in stop_position):
            raise ValueError("Non-finite waypoint coordinates")

        nearest_index = min(range(previous_index, len(route)),
                            key=lambda index: distance_meters(route[index], stop_position))
        distance_from_route = distance_meters(route[nearest_index], stop_position)
        if distance_from_route > MAX_STOP_DISTANCE_FROM_ROUTE_METERS:
            warnings.append("Waypoint ignored: %.0f m from route" % distance_from_route)
            continue
        if nearest_index <= previous_index or nearest_index == len(route) - 1:
            warnings.append("Waypoint ignored: same route vertex as previous anchor or trip endpoint")
            continue

        anchors.append((nearest_index, stop_time))
        previous_index = nearest_index
    return anchors


def time_at_stop(stop, scheduled_start, scheduled_end, departure_delay, arrival_delay):
    # A realtime stop time already includes the delay, so it is used as it is.
    for key in ("arr_rt", "dep_rt"):
        if str(stop.get(key) or "").strip():
            return parse_utc_timestamp(stop[key], key)

    # A scheduled stop time gets the delay, interpolated between departure and arrival delay.
    for key in ("arr", "dep"):
        if str(stop.get(key) or "").strip():
            scheduled_time = parse_utc_timestamp(stop[key], key)
            progress = max(0, min(1, (scheduled_time - scheduled_start) / (scheduled_end - scheduled_start)))
            return scheduled_time + departure_delay + progress * (arrival_delay - departure_delay)
    return None


def fit_anchor_times(anchors, distance_along_route, average_speed):
    """Whole-second anchor times that strictly increase, each within the forward-only
    minute window of its preferred time. None if that's impossible.

    Two stops can share a minute (e.g. both "10:01"), so each section between anchors
    also gets a minimum duration: it may not be faster than MAX_SECTION_SPEED_FACTOR
    times the trip's average speed. If the minute windows don't allow that, the fastest
    section is made as slow as the windows allow."""
    section_distances = [distance_along_route[end] - distance_along_route[start]
                         for (start, _), (end, _) in zip(anchors, anchors[1:])]

    def minimum_durations(speed_limit):
        return [max(1, math.ceil(distance / speed_limit)) for distance in section_distances]

    def time_ranges(durations):
        earliest = [math.ceil(time) for _, time in anchors]
        latest = [math.floor(time + ANCHOR_TIME_WINDOW_SECONDS) for _, time in anchors]
        for i in range(1, len(earliest)):
            earliest[i] = max(earliest[i], earliest[i - 1] + durations[i - 1])
        for i in range(len(latest) - 2, -1, -1):
            latest[i] = min(latest[i], latest[i + 1] - durations[i])
        if any(low > high for low, high in zip(earliest, latest)):
            return None
        return earliest, latest

    if time_ranges([1] * len(section_distances)) is None:
        return None

    speed_limit = MAX_SECTION_SPEED_FACTOR * average_speed
    if time_ranges(minimum_durations(speed_limit)) is None:
        # Find the lowest speed limit the minute windows allow. At this speed every
        # section needs at most 1 second, which always fits.
        too_slow, fast_enough = speed_limit, max(section_distances)
        for _ in range(60):
            middle = (too_slow + fast_enough) / 2
            if time_ranges(minimum_durations(middle)) is None:
                too_slow = middle
            else:
                fast_enough = middle
        speed_limit = fast_enough
    durations = minimum_durations(speed_limit)
    earliest, latest = time_ranges(durations)

    chosen = []
    is_endpoint = {0, len(anchors) - 1}
    for i, (_, time) in enumerate(anchors):
        # Trip endpoints prefer their exact time, stops the middle of their minute.
        preferred = round(time) if i in is_endpoint else math.floor(time + ANCHOR_TIME_WINDOW_SECONDS / 2)
        minimum = max(earliest[i], chosen[-1] + durations[i - 1] if chosen else earliest[i])
        chosen.append(min(latest[i], max(minimum, preferred)))
    return chosen


def interpolate_times(route, distance_along_route, anchor_indices, anchor_times):
    """A timestamp for every route vertex, spread by distance between the anchors."""
    times = []
    for index in range(len(route)):
        section = bisect.bisect_right(anchor_indices, index) - 1
        section = max(0, min(len(anchor_indices) - 2, section))
        section_start, section_end = anchor_indices[section], anchor_indices[section + 1]

        section_length = distance_along_route[section_end] - distance_along_route[section_start]
        if section_length:
            progress = (distance_along_route[index] - distance_along_route[section_start]) / section_length
        else:
            progress = (index - section_start) / (section_end - section_start)

        start_time, end_time = anchor_times[section], anchor_times[section + 1]
        times.append(round(start_time + progress * (end_time - start_time)))
    return times


# --- Geometry -----------------------------------------------------------------------


def distance_meters(a, b):
    """Great-circle distance between two [longitude, latitude] positions."""
    longitude_a, latitude_a, longitude_b, latitude_b = map(math.radians, (*a, *b))
    haversine = (math.sin((latitude_b - latitude_a) / 2) ** 2
                 + math.cos(latitude_a) * math.cos(latitude_b) * math.sin((longitude_b - longitude_a) / 2) ** 2)
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1, max(0, haversine))))


def distance_to_segment_meters(position, segment_start, segment_end):
    """Distance from a [longitude, latitude] position to a straight segment, on a flat local map."""
    reference_latitude = math.radians((segment_start[1] + segment_end[1]) / 2)

    def to_meters(other):
        longitude_difference = (other[0] - segment_start[0] + 180) % 360 - 180
        return (EARTH_RADIUS_METERS * math.radians(longitude_difference) * math.cos(reference_latitude),
                EARTH_RADIUS_METERS * math.radians(other[1] - segment_start[1]))

    point_x, point_y = to_meters(position)
    end_x, end_y = to_meters(segment_end)
    length_squared = end_x * end_x + end_y * end_y
    progress = max(0, min(1, (point_x * end_x + point_y * end_y) / length_squared)) if length_squared else 0
    return math.hypot(point_x - progress * end_x, point_y - progress * end_y)


def simplify_route(route, times, anchor_indices):
    """Indices of the route vertices to keep (Douglas-Peucker between fixed vertices)."""
    must_keep = {0, len(route) - 1, *anchor_indices}

    last_checkpoint = 0
    for index in range(1, len(route)):
        if index in must_keep or times[index] - times[last_checkpoint] >= CHECKPOINT_INTERVAL_SECONDS:
            must_keep.add(index)
            last_checkpoint = index

    keep = set(must_keep)
    fixed = sorted(must_keep)
    sections_to_check = list(zip(fixed, fixed[1:]))
    while sections_to_check:
        left, right = sections_to_check.pop()
        if right <= left + 1:
            continue
        farthest_index, farthest_distance = max(
            ((index, distance_to_segment_meters(route[index], route[left], route[right]))
             for index in range(left + 1, right)),
            key=lambda pair: pair[1],
        )
        if farthest_distance > SIMPLIFY_TOLERANCE_METERS:
            keep.add(farthest_index)
            sections_to_check.extend(((left, farthest_index), (farthest_index, right)))
    return sorted(keep)


def drop_points_sharing_a_second(kept_indices, times, anchor_indices):
    """Dawarich orders points by timestamp, so points in the same second can be drawn in
    the wrong order. Keeps every anchor and drops other vertices that would share a second
    with the point before them or with the next anchor."""
    anchor_set = set(anchor_indices)
    result = []
    for index in kept_indices:
        if index not in anchor_set:
            next_anchor = anchor_indices[bisect.bisect_right(anchor_indices, index)]
            if times[index] <= times[result[-1]] or times[index] >= times[next_anchor]:
                continue
        result.append(index)
    return result
