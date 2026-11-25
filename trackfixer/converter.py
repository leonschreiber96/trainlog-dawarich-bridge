"""Turn one trainlog trip (a row of trainlog's CSV export) into timed route points.

Ported from csv_to_dawarich.py with the same default settings and the same results.

How the timestamps are made:
* The trip starts at utc_start_datetime + departure_delay and ends at
  utc_end_datetime + arrival_delay (delays are in seconds).
* Stops along the way (trainlog "waypoints") with a known time become extra time
  anchors. Realtime stop times are used as they are; scheduled stop times get the
  delay, interpolated between the departure and arrival delay. Where a stop's departure
  is in a later minute than its arrival, the ride waits there: the stop's vertex is
  doubled, with the arrival time on the first and the departure time on the second.
* trainlog times are only minute-precise, so an anchor may move forward by up to
  59 seconds to keep all anchor times strictly increasing.
* Two stops can even share a minute. So a section between two anchors may not be
  faster than twice the trip's average speed while moving (waiting at stops not
  counted); if the minute windows don't allow that, the fastest section is made as slow
  as they allow.
* Exact anchors (see phone_anchors.py: where a phone point with good accuracy shows the
  ride was at what second) get a vertex of their own on the route and keep their time to
  the second. The speed limit above doesn't apply next to them: a ride that stood still
  for a while really was fast before or after. Where they contradict a scheduled stop
  time, the stop time is left out (see combine_anchors). Without exact anchors, a trip
  converts exactly as it did before they existed.
* Between two anchors, time is spread over the route by distance.
* Finally the route is simplified: a vertex is only kept if it is needed to stay
  close enough to the real route (SIMPLIFY_TOLERANCE_METERS), if it is an anchor, or as a
  checkpoint roughly every 120 seconds. No two points share a second.
"""

import bisect
import json
import math
from datetime import datetime, timezone
from typing import NamedTuple

from .config import tuning_number

ENCODED_PATH_PRECISION = 5
ANCHOR_TIME_WINDOW_SECONDS = 59
MAX_SECTION_SPEED_FACTOR = 2
# For trainlog trips. Drawn trips have their own tolerance, see trip_sync.py.
SIMPLIFY_TOLERANCE_METERS = tuning_number("TRAINLOG_ROUTE_TOLERANCE_METERS", 25)
CHECKPOINT_INTERVAL_SECONDS = tuning_number("ROUTE_CHECKPOINT_SECONDS", 120)
MAX_STOP_DISTANCE_FROM_ROUTE_METERS = 500
EARTH_RADIUS_METERS = 6_371_000

# A scheduled stop time is left out if exact anchors next to it put the ride there more than this
# much before or after its minute (the delay wasn't entered in trainlog, or was wrong).
SCHEDULED_STOP_MAX_DEVIATION_SECONDS = tuning_number("PHONE_ANCHOR_MIN_DEVIATION_SECONDS", 60)
# An exact anchor this close to a route vertex uses that vertex instead of getting its own.
EXACT_ANCHOR_SNAP_METERS = 0.05

# Kinds of anchor:
ENDPOINT = "endpoint"  # where the trip starts or ends: minute-precise
STOP = "stop"  # a stop with a time in trainlog: minute-precise
EXACT = "exact"  # known to the second


class Anchor(NamedTuple):
    """The time the ride was at a route vertex."""
    index: int
    time: float
    kind: str
    realtime: bool = False  # for stops: what really happened (see time_at_stop), not a scheduled guess

    @property
    def window_seconds(self):
        return 0 if self.kind == EXACT else ANCHOR_TIME_WINDOW_SECONDS


class Stay(NamedTuple):
    """A stop with a time in trainlog: when the ride arrived there and when it left (minute-precise)."""
    index: int  # the route vertex of its arrival
    arrival: float
    departure: float  # the same as arrival, unless the ride left in a later minute
    realtime: bool


class TripBasis(NamedTuple):
    """What a trip's timing is made from."""
    route: list  # [longitude, latitude] vertices; a stop's vertex is doubled where the ride waited there
    distance_along_route: list  # meters from the start to each vertex
    start: float  # delay-adjusted, as a timestamp
    end: float
    stops: list  # Anchors of the stops' arrivals and departures, in route order
    stays: list  # the stops as Stays, in route order

    @property
    def moving_seconds(self):
        """How long the ride was moving: its duration without the waiting at stops
        (at least a quarter of it, in case trainlog's stop times are off)."""
        duration = self.end - self.start
        waiting = sum(stay.departure - stay.arrival for stay in self.stays)
        return max(duration - waiting, duration / 4)


def convert_trip(row, simplify_tolerance_meters=SIMPLIFY_TOLERANCE_METERS, exact_anchors=()):
    """Returns (route points, warnings). Raises ValueError if the trip can't be converted.

    Each route point is a dict with "timestamp", "latitude" and "longitude".
    exact_anchors: [[timestamp, meters along the route], ...] in route order, from
    phone_anchors.choose_exact_anchors.
    """
    warnings = []
    route, times, anchor_indices = timed_route(row, warnings, exact_anchors)
    kept_indices = simplify_route(route, times, anchor_indices, simplify_tolerance_meters)
    kept_indices = drop_points_sharing_a_second(kept_indices, times, anchor_indices)

    route_points = [
        {"timestamp": times[index], "latitude": route[index][1], "longitude": route[index][0]}
        for index in kept_indices
    ]
    return route_points, warnings


def trip_basis(row, warnings):
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

    stays = stays_at_stops(row, route, scheduled_start, scheduled_end, departure_delay, arrival_delay, warnings)
    route, stops, stays = wait_at_stops(route, stays)
    return TripBasis(route, distances_along(route), actual_start, actual_end, stops, stays)


def wait_at_stops(route, stays):
    """(route, stop anchors, stays) with each stop's vertex doubled where the ride waited there,
    so it can stand still from the arrival on the first to the departure on the second."""
    stay_at_vertex = {stay.index: stay for stay in stays}
    new_route, anchors, new_stays = [], [], []
    for index, vertex in enumerate(route):
        new_route.append(vertex)
        stay = stay_at_vertex.get(index)
        if stay is None:
            continue
        arrival_index = len(new_route) - 1
        anchors.append(Anchor(arrival_index, stay.arrival, STOP, stay.realtime))
        if stay.departure >= stay.arrival + 60:
            new_route.append(list(vertex))
            anchors.append(Anchor(len(new_route) - 1, stay.departure, STOP, stay.realtime))
        else:
            stay = stay._replace(departure=stay.arrival)
        new_stays.append(stay._replace(index=arrival_index))
    return new_route, anchors, new_stays


def trip_time_range(row):
    """(start, end) of the ride as timestamps, delays included. Raises ValueError."""
    start = parse_utc_timestamp(row.get("utc_start_datetime"), "utc_start_datetime")
    end = parse_utc_timestamp(row.get("utc_end_datetime"), "utc_end_datetime")
    return start + parse_delay_seconds(row.get("departure_delay")), end + parse_delay_seconds(row.get("arrival_delay"))


def distances_along(route):
    distance_along_route = [0.0]
    for vertex, next_vertex in zip(route, route[1:]):
        distance_along_route.append(distance_along_route[-1] + distance_meters(vertex, next_vertex))
    return distance_along_route


def timed_route(row, warnings, exact_anchors=()):
    """(route vertices, a timestamp for each, indices of the anchor vertices)."""
    basis = trip_basis(row, warnings)
    route, distance_along_route, stops = basis.route, basis.distance_along_route, basis.stops
    start = Anchor(0, basis.start, ENDPOINT)

    exact = []
    if exact_anchors:
        route, distance_along_route, exact, new_index_of_vertex = insert_exact_anchor_vertices(
            route, distance_along_route, exact_anchors)
        stops = [stop._replace(index=new_index_of_vertex[stop.index]) for stop in stops]
    end = Anchor(len(route) - 1, basis.end, ENDPOINT)
    if exact_anchors:
        stops, exact, notes = combine_anchors(start, end, stops, exact, distance_along_route)
        warnings += notes

    # An anchor is the time the train was at a route vertex.
    anchors = [start] + sorted(stops + exact, key=lambda anchor: anchor.index) + [end]

    average_speed = distance_along_route[-1] / basis.moving_seconds
    anchor_times = fit_anchor_times(anchors, distance_along_route, average_speed)
    if anchor_times is None and exact:
        warnings.append("Inconsistent waypoint timing; using the phone's points and the trip endpoints only")
        anchors = [start] + exact + [end]
        anchor_times = fit_anchor_times(anchors, distance_along_route, average_speed)
    if anchor_times is None:
        warnings.append("Inconsistent waypoint timing; using delay-adjusted trip endpoints only")
        anchors = [anchors[0], anchors[-1]]
        anchor_times = fit_anchor_times(anchors, distance_along_route, average_speed)
    if anchor_times is None:
        raise ValueError("Cannot reconcile endpoint times")

    if anchor_times[0] != round(basis.start) or anchor_times[-1] != round(basis.end):
        warnings.append(
            "Endpoint adjusted inside minute window: departure %+ds, arrival %+ds"
            % (anchor_times[0] - round(basis.start), anchor_times[-1] - round(basis.end))
        )

    anchor_indices = [anchor.index for anchor in anchors]
    return route, interpolate_times(route, distance_along_route, anchor_indices, anchor_times), anchor_indices


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


def encode_path(vertices):
    """Encode [longitude, latitude] vertices as a Google encoded polyline (what decode_path reads)."""
    def encode_number(number):
        number = ~(number << 1) if number < 0 else number << 1
        characters = []
        while number >= 32:
            characters.append(chr((32 | (number & 31)) + 63))
            number >>= 5
        characters.append(chr(number + 63))
        return "".join(characters)

    encoded = []
    previous_latitude = previous_longitude = 0
    for longitude, latitude in vertices:
        latitude = round(latitude * 10**ENCODED_PATH_PRECISION)
        longitude = round(longitude * 10**ENCODED_PATH_PRECISION)
        encoded.append(encode_number(latitude - previous_latitude) + encode_number(longitude - previous_longitude))
        previous_latitude, previous_longitude = latitude, longitude
    return "".join(encoded)


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


def stays_at_stops(row, route, scheduled_start, scheduled_end, departure_delay, arrival_delay, warnings):
    """The stops along the trip with a time, as Stays at their nearest route vertex, in route order."""
    waypoints = parse_waypoints(row.get("waypoints"))
    for waypoint in waypoints:
        if not isinstance(waypoint, dict):
            raise ValueError("Waypoint must be an object")
        if not isinstance(waypoint.get("stop") or {}, dict):
            raise ValueError("Waypoint stop must be an object")
    trip_has_realtime = any(has_time(waypoint.get("stop") or {}, key)
                            for waypoint in waypoints for key in ("arr_rt", "dep_rt"))

    stays = []
    previous_index = 0
    for waypoint in waypoints:
        stop = waypoint.get("stop") or {}
        arrival, departure, is_realtime = time_at_stop(
            stop, scheduled_start, scheduled_end, departure_delay, arrival_delay, trip_has_realtime)
        latitude = waypoint.get("lat", stop.get("lat"))
        longitude = waypoint.get("lng", stop.get("lng"))
        if arrival is None or latitude is None or longitude is None:
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

        stays.append(Stay(nearest_index, arrival, departure, is_realtime))
        previous_index = nearest_index
    return stays


def has_time(stop, key):
    return bool(str(stop.get(key) or "").strip())


def time_at_stop(stop, scheduled_start, scheduled_end, departure_delay, arrival_delay, trip_has_realtime):
    """(arrival, departure, whether that's what really happened), or (None, None, False) if the stop has
    no time. A stop with only one of the two gets it for both.

    trainlog stores a realtime time only where it differs from the schedule, for arrival and departure
    separately. So in a trip with realtime times, a stop's scheduled time is what happened wherever it has
    no realtime one (the train was on time there). Only in a trip without any realtime times are the
    trip's delays spread over its scheduled stop times."""
    def has(key):
        return has_time(stop, key)

    if trip_has_realtime:
        arrival_key = next((key for key in ("arr_rt", "arr") if has(key)), None)
        departure_key = next((key for key in ("dep_rt", "dep") if has(key)), None)
        if arrival_key is None and departure_key is None:
            return None, None, False
        arrival = parse_utc_timestamp(stop[arrival_key or departure_key], arrival_key or departure_key)
        departure = parse_utc_timestamp(stop[departure_key], departure_key) if departure_key else arrival
        return arrival, max(arrival, departure), True

    # Scheduled stop times get the delay, interpolated between departure and arrival delay.
    def with_delay(key):
        scheduled_time = parse_utc_timestamp(stop[key], key)
        progress = max(0, min(1, (scheduled_time - scheduled_start) / (scheduled_end - scheduled_start)))
        return scheduled_time + departure_delay + progress * (arrival_delay - departure_delay)

    if has("arr") or has("dep"):
        arrival = with_delay("arr" if has("arr") else "dep")
        departure = with_delay("dep") if has("dep") else arrival
        return arrival, departure, False
    return None, None, False


def fit_anchor_times(anchors, distance_along_route, average_speed):
    """Whole-second anchor times that strictly increase, each within the forward-only
    minute window of its preferred time (exact anchors: at their time). None if that's impossible.

    Two stops can share a minute (e.g. both "10:01"), so each section between two
    minute-precise anchors also gets a minimum duration: it may not be faster than
    MAX_SECTION_SPEED_FACTOR times the trip's average speed. If the minute windows don't
    allow that, the fastest section is made as slow as the windows allow."""
    section_distances = [distance_along_route[end.index] - distance_along_route[start.index]
                         for start, end in zip(anchors, anchors[1:])]
    is_capped = [EXACT not in (start.kind, end.kind) for start, end in zip(anchors, anchors[1:])]

    def minimum_durations(speed_limit):
        return [max(1, math.ceil(distance / speed_limit)) if capped else 1
                for distance, capped in zip(section_distances, is_capped)]

    def time_ranges(durations):
        earliest = [math.ceil(anchor.time) for anchor in anchors]
        latest = [math.floor(anchor.time + anchor.window_seconds) for anchor in anchors]
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
    for i, anchor in enumerate(anchors):
        # Trip endpoints and exact anchors prefer their exact time, stops the middle of their minute.
        if anchor.kind == STOP:
            preferred = math.floor(anchor.time + ANCHOR_TIME_WINDOW_SECONDS / 2)
        else:
            preferred = round(anchor.time)
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


# --- Exact anchors ------------------------------------------------------------------


def insert_exact_anchor_vertices(route, distance_along_route, exact_anchors):
    """The route with a vertex where each exact anchor is (or the vertex already there).

    exact_anchors: [[timestamp, meters along the route], ...]. Those not strictly after the
    previous one (in time and along the route), or not between the trip's endpoints, are left out.
    Returns (route, distance along it, the exact Anchors, the new index of each original vertex)."""
    total_meters = distance_along_route[-1]
    last_index = len(route) - 1
    at_vertex = {}  # original vertex index -> timestamp
    inside_segment = {}  # index of the segment's first vertex -> [(meters, timestamp)]
    previous = None
    for timestamp, meters in exact_anchors:
        timestamp, meters = int(timestamp), float(meters)
        if not 0 < meters < total_meters or (previous and (timestamp <= previous[0] or meters <= previous[1])):
            continue
        segment = min(bisect.bisect_right(distance_along_route, meters) - 1, last_index - 1)
        if meters - distance_along_route[segment] <= EXACT_ANCHOR_SNAP_METERS:
            vertex = segment
        elif distance_along_route[segment + 1] - meters <= EXACT_ANCHOR_SNAP_METERS:
            vertex = segment + 1
        else:
            vertex = None
        if vertex is None:
            inside_segment.setdefault(segment, []).append((meters, timestamp))
        elif vertex in at_vertex or vertex in (0, last_index):
            continue
        else:
            at_vertex[vertex] = timestamp
        previous = (timestamp, meters)

    new_route, new_index_of_vertex, exact = [], [], []
    for index, vertex in enumerate(route):
        new_index_of_vertex.append(len(new_route))
        if index in at_vertex:
            exact.append(Anchor(len(new_route), at_vertex[index], EXACT))
        new_route.append(vertex)
        for meters, timestamp in inside_segment.get(index, []):
            following = route[index + 1]
            share = (meters - distance_along_route[index]) / (distance_along_route[index + 1] - distance_along_route[index])
            exact.append(Anchor(len(new_route), timestamp, EXACT))
            new_route.append([round(vertex[0] + share * (following[0] - vertex[0]), 7),
                              round(vertex[1] + share * (following[1] - vertex[1]), 7)])
    return new_route, distances_along(new_route), exact, new_index_of_vertex


def combine_anchors(start, end, stops, exact, distance_along_route):
    """Which stops and exact anchors to use together. Returns (stops, exact anchors, warnings).

    The anchors' indices are vertices of the route that distance_along_route is for.
    * An exact anchor that doesn't fit between the trip's endpoints is left out.
    * One that contradicts a realtime stop time (in which order the ride was where) is left
      out too: trainlog's realtime times are reliable, so the phone point probably isn't.
    * A scheduled stop time is left out if it contradicts an exact anchor, or if the exact
      anchors next to it put the ride there more than SCHEDULED_STOP_MAX_DEVIATION_SECONDS
      before or after its minute (its delay wasn't entered in trainlog, or was wrong)."""
    def meters(anchor):
        return distance_along_route[anchor.index]

    def can_be_in_order(before, after):
        """Can `before` get an earlier second than `after`, at a vertex before it?"""
        if meters(before) >= meters(after):
            return False
        return math.ceil(before.time) < math.floor(after.time + after.window_seconds)

    def fits_with(anchor, other):
        if anchor.index == other.index:
            return False
        if meters(anchor) < meters(other):
            return can_be_in_order(anchor, other)
        return can_be_in_order(other, anchor)

    warnings = []
    realtime_stops = [stop for stop in stops if stop.realtime]
    exact = [anchor for anchor in exact if can_be_in_order(start, anchor) and can_be_in_order(anchor, end)]
    fitting = [anchor for anchor in exact if all(fits_with(anchor, stop) for stop in realtime_stops)]
    if len(fitting) < len(exact):
        warnings.append(f"{len(exact) - len(fitting)} phone point(s) contradicted trainlog's realtime stop times "
                        "and weren't used")
    exact = fitting

    strong = sorted([start, end] + realtime_stops + exact, key=meters)
    kept_stops, contradicted = [], 0
    for stop in stops:
        if not stop.realtime:
            before = max((anchor for anchor in strong if meters(anchor) < meters(stop)), key=meters, default=None)
            after = min((anchor for anchor in strong if meters(anchor) > meters(stop)), key=meters, default=None)
            if not all(fits_with(stop, anchor) for anchor in exact) or (
                    before is not None and after is not None and EXACT in (before.kind, after.kind)
                    and not scheduled_stop_agrees(stop, before, after, meters)):
                contradicted += 1
                continue
        kept_stops.append(stop)
    if contradicted:
        warnings.append(f"{contradicted} scheduled stop time(s) contradicted the phone's points and weren't used")
    return kept_stops, exact, warnings


def likely_time(anchor):
    """When the ride was most likely at an anchor: in the middle of a stop's minute, otherwise at its time."""
    return anchor.time + ANCHOR_TIME_WINDOW_SECONDS / 2 if anchor.kind == STOP else anchor.time


def scheduled_stop_agrees(stop, before, after, meters):
    """Does the time between the anchors before and after the stop (spread by distance) fall
    into the stop's minute, give or take SCHEDULED_STOP_MAX_DEVIATION_SECONDS?"""
    share = (meters(stop) - meters(before)) / (meters(after) - meters(before))
    time_there = likely_time(before) + share * (likely_time(after) - likely_time(before))
    return (stop.time - SCHEDULED_STOP_MAX_DEVIATION_SECONDS
            <= time_there <= stop.time + ANCHOR_TIME_WINDOW_SECONDS + SCHEDULED_STOP_MAX_DEVIATION_SECONDS)


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


def simplify_route(route, times, anchor_indices, tolerance_meters=SIMPLIFY_TOLERANCE_METERS):
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
        if farthest_distance > tolerance_meters:
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
