"""Timing a trip with the phone's own points ("exact anchors").

A trip's times are only known at its start and end, and at stops with a time in trainlog.
In between, the converter spreads time evenly by distance. That's wrong when the ride
wasn't steady: a train that stood still between two stops, a car in a traffic jam. The
phone points recorded during the ride show what really happened, even though the line
the phone draws between them is bad (cut corners, tunnels). So before they're deleted,
the accurate ones are kept as "phone fixes" of the trip (database.phone_fixes), and some
of them become exact anchors: the second at which the ride was at a place on the route.

Which fixes become anchors (assess_phone_fixes), from the stored fixes each time the trip
is converted, so editing a drawn route, or more fixes arriving late, chooses again:
* App points only (the Shortcut's have no accuracy and may be old), inside the trip's time, within
  PHONE_ANCHOR_MAX_DISTANCE_FROM_ROUTE_METERS of the route. Where the route passes a place
  twice, the pass closer to where the ride should be at that time counts.
* With an accuracy of PHONE_ANCHOR_MAX_ACCURACY_METERS or better, or, as phones in trains often
  report kilometers for points right on the tracks, at most PHONE_ANCHOR_INACCURATE_MAX_MINUTES_OFF
  before or after the trip's own times have the ride there (a point really that far off that
  happens to lie on the route is mostly far from that).
* Not while waiting at a stop: a fix near a stop (STATION_RADIUS_METERS, platforms are long)
  between its arrival and departure in trainlog adds nothing, and GPS noise can put it
  just before the stop, which would look like a contradiction.
* In route order, at plausible speeds: the largest set of fixes that never goes back along
  the route (more than GPS noise) or needs more than a top speed for the kind of trip.
* Only fixes that change something: the one farthest off the times the trip would get
  without it becomes an anchor, as long as that's more than PHONE_ANCHOR_MIN_DEVIATION_SECONDS.
  So a ride that went as planned gets no anchors and converts as before.
"""

import bisect
import math
from typing import NamedTuple

from .config import tuning_number
from .converter import (ANCHOR_TIME_WINDOW_SECONDS, ENDPOINT, Anchor, combine_anchors, insert_exact_anchor_vertices,
                        likely_time, trip_basis)
from .phone_points import STATION_RADIUS_METERS, is_trip_point

MAX_ACCURACY_METERS = tuning_number("PHONE_ANCHOR_MAX_ACCURACY_METERS", 30)
MAX_DISTANCE_FROM_ROUTE_METERS = tuning_number("PHONE_ANCHOR_MAX_DISTANCE_FROM_ROUTE_METERS", 50)
MIN_DEVIATION_SECONDS = tuning_number("PHONE_ANCHOR_MIN_DEVIATION_SECONDS", 60)
# A fix the phone reports as less accurate than MAX_ACCURACY_METERS still counts if it's on the route (within
# MAX_DISTANCE_FROM_ROUTE_METERS) at most this long before or after the trip's own times (start, end, stops)
# have the ride there. Phones in trains often report kilometers for points right on the tracks. One that
# really is that far off and happens to lie on the route is mostly far from where the ride was at that time.
INACCURATE_MAX_TIME_OFF_SECONDS = tuning_number("PHONE_ANCHOR_INACCURATE_MAX_MINUTES_OFF", 15) * 60

# Faster than this between two fixes (along the route) is implausible, so one of them is wrong.
# Trips faster on average than this are allowed 1.5 times their average speed.
MAX_SPEED_KMH_BY_TRIP_TYPE = {
    "walk": 15, "run": 30, "cycle": 70, "car": 250, "bus": 150, "tram": 100, "metro": 120,
    "funicular": 60, "ferry": 90,
}
DEFAULT_MAX_SPEED_KMH = 350  # trains
# Phones on planes are mostly in flight mode, and their points far from the route anyway.
TRIP_TYPES_WITHOUT_PHONE_ANCHORS = {"air", "helicopter"}

# Two fixes may go this far back along the route (GPS noise while standing still), plus twice their accuracy.
BACKTRACK_TOLERANCE_METERS = 5
# Fixes at the same place (standing still) are spread this far apart along the route,
# so each gets a vertex of its own.
MIN_METERS_BETWEEN_ANCHORS = 0.5
MAX_FIXES_PER_TRIP = 3000  # beyond that, every n-th one (choosing is quadratic in their number)

# What became of a fix (assess_phone_fixes):
USED = "used"  # it's an anchor
NOT_NEEDED = "not needed"  # plausible, but the trip's times were within MIN_DEVIATION_SECONDS of it anyway
IGNORED = "ignored"  # not to be used, chosen on the drawn trip's map
UNUSABLE = "unusable"  # not accurate enough, outside the trip's time, off the route, or implausible
AT_A_STOP = "at a stop"  # recorded while waiting at a stop, which trainlog's times already cover
CONTRADICTS_STOP = "contradicts a realtime stop time"


class Candidate(NamedTuple):
    timestamp: int
    meters: float  # along the route
    accuracy: float


def fixes_from_points(points, start_timestamp, end_timestamp):
    """The phone fixes among Dawarich points: app points inside the trip's time, however accurate the phone
    says they are (assess_phone_fixes decides), including those Dawarich flagged as GPS noise for that."""
    fixes = []
    for point in points:
        accuracy = point.get("accuracy")
        if is_trip_point(point) or accuracy is None or not start_timestamp < point["timestamp"] < end_timestamp:
            continue
        try:
            accuracy = float(accuracy)
        except (TypeError, ValueError):
            continue
        if 0 <= accuracy:
            fixes.append({"timestamp": int(point["timestamp"]), "latitude": float(point["latitude"]),
                          "longitude": float(point["longitude"]), "accuracy": accuracy})
    return fixes


def choose_exact_anchors(row, fixes):
    """[[timestamp, meters along the route], ...] for converter.convert_trip."""
    return assess_phone_fixes(row, fixes)[0]


def assess_phone_fixes(row, fixes):
    """(exact anchors, {fix timestamp: what became of it}). Raises ValueError if the trip can't be converted."""
    ignored = {int(timestamp) for timestamp in row.get("ignored_phone_fixes") or []}
    statuses = {int(fix["timestamp"]): IGNORED if int(fix["timestamp"]) in ignored else UNUSABLE for fix in fixes}
    if not fixes or row.get("type") in TRIP_TYPES_WITHOUT_PHONE_ANCHORS:
        return [], statuses

    basis = trip_basis(row, [])
    route, distance_along_route = basis.route, basis.distance_along_route
    total_meters = distance_along_route[-1]
    start = Anchor(0, basis.start, ENDPOINT)
    end = Anchor(len(route) - 1, basis.end, ENDPOINT)
    times_without_fixes = timeline(start, end, basis.stops, [], distance_along_route)
    route_index = RouteIndex(route, distance_along_route)

    candidates = []
    for fix in sorted(fixes, key=lambda fix: fix["timestamp"]):
        timestamp = int(fix["timestamp"])
        if timestamp in ignored or fix.get("accuracy") is None or not basis.start < timestamp < basis.end:
            continue
        meters = route_index.meters_along(
            [float(fix["longitude"]), float(fix["latitude"])], meters_at(times_without_fixes, timestamp))
        if meters is None:
            continue
        accuracy = float(fix["accuracy"])
        if accuracy > MAX_ACCURACY_METERS:
            if abs(time_at(times_without_fixes, meters) - timestamp) > INACCURATE_MAX_TIME_OFF_SECONDS:
                continue
            accuracy = MAX_DISTANCE_FROM_ROUTE_METERS  # as far as it's trusted, for the checks below
        if waiting_at_a_stop(basis, timestamp, meters, accuracy):
            statuses[timestamp] = AT_A_STOP
            continue
        candidates.append(Candidate(timestamp, meters, accuracy))
    if len(candidates) > MAX_FIXES_PER_TRIP:
        step = math.ceil(len(candidates) / MAX_FIXES_PER_TRIP)
        candidates = candidates[::step]

    max_speed_kmh = MAX_SPEED_KMH_BY_TRIP_TYPE.get(row.get("type"), DEFAULT_MAX_SPEED_KMH)
    max_speed = max(max_speed_kmh / 3.6, 1.5 * total_meters / (basis.end - basis.start))
    chain = spread_out(plausible_chain(candidates, basis.start, basis.end, total_meters, max_speed), total_meters)

    def times_with(selected):
        """(The trip's timeline with these fixes as anchors, those of them left out for contradicting a stop)."""
        times, left_out = timeline_with(basis, [[candidate.timestamp, candidate.meters] for candidate in selected])
        return times, [candidate for candidate in selected if candidate.timestamp in left_out]

    selected, remaining = [], list(chain)
    while True:
        times_now, left_out = times_with(selected)
        if left_out:
            for candidate in left_out:
                selected.remove(candidate)
                statuses[candidate.timestamp] = CONTRADICTS_STOP
            continue
        if not remaining:
            break
        deviation, farthest = max(((abs(time_at(times_now, candidate.meters) - candidate.timestamp), candidate)
                                   for candidate in remaining), key=lambda pair: (pair[0], -pair[1].timestamp))
        if deviation <= MIN_DEVIATION_SECONDS:
            break
        remaining.remove(farthest)
        selected = sorted(selected + [farthest], key=lambda candidate: candidate.timestamp)

    for candidate in chain:
        if statuses[candidate.timestamp] == UNUSABLE:
            statuses[candidate.timestamp] = NOT_NEEDED
    for candidate in selected:
        statuses[candidate.timestamp] = USED
    return [[candidate.timestamp, round(candidate.meters, 1)] for candidate in selected], statuses


def explain_phone_fixes(row, fixes):
    """For looking at a trip's timing by hand (the trip page): its route and stops, and for each phone fix
    what became of it, where it is along the route, when trainlog's own times (start, end, stops, without
    any phone fix) and the trip's times (with the phone fixes it's timed with) have the ride there, and
    which realtime stop times it contradicts.
    Raises ValueError if the trip can't be converted."""
    exact_anchors, statuses = assess_phone_fixes(row, fixes)
    basis = trip_basis(row, [])
    route, distance_along_route = basis.route, basis.distance_along_route
    start = Anchor(0, basis.start, ENDPOINT)
    end = Anchor(len(route) - 1, basis.end, ENDPOINT)
    times = timeline(start, end, basis.stops, [], distance_along_route)
    timed_times = timeline_with(basis, exact_anchors)[0] if exact_anchors else times
    route_index = RouteIndex(route, distance_along_route)

    stops = [
        {"name": stay.name or f"Stop {number}", "meters": distance_along_route[stay.index],
         "latitude": route[stay.index][1], "longitude": route[stay.index][0],
         "arrival": stay.arrival, "departure": stay.departure, "realtime": stay.realtime}
        for number, stay in enumerate(basis.stays, 1)
    ]

    explained = []
    for fix in sorted(fixes, key=lambda fix: fix["timestamp"]):
        timestamp = int(fix["timestamp"])
        accuracy = float(fix["accuracy"]) if fix.get("accuracy") is not None else None
        inside = basis.start < timestamp < basis.end
        meters = route_index.meters_along([float(fix["longitude"]), float(fix["latitude"])],
                                          meters_at(times, timestamp)) if inside else None
        trainlog_time = time_at(times, meters) if meters is not None else None
        place_then = position_at(route, distance_along_route, meters_at(times, timestamp)) if inside else None
        status = statuses.get(timestamp, UNUSABLE)
        explained.append({
            "timestamp": timestamp, "latitude": float(fix["latitude"]), "longitude": float(fix["longitude"]),
            "accuracy": accuracy, "status": status,
            "why": why_unusable(timestamp, accuracy, inside, meters, trainlog_time) if status == UNUSABLE else "",
            "meters": meters, "trainlog_time": trainlog_time,
            # when the trip's times, with the phone fixes it's timed with, have the ride there
            "timed_time": time_at(timed_times, meters) if meters is not None else None,
            # where trainlog's times have the ride when the phone recorded the fix
            "trainlog_latitude": place_then[1] if place_then else None,
            "trainlog_longitude": place_then[0] if place_then else None,
            "contradicted_stops": contradicted_stops(timestamp, meters, stops) if meters is not None else [],
        })

    return {"route": [[latitude, longitude] for longitude, latitude in route], "start": basis.start,
            "end": basis.end, "total_meters": distance_along_route[-1], "stops": stops, "fixes": explained}


def why_unusable(timestamp, accuracy, inside_trip_time, meters, trainlog_time):
    if accuracy is None:
        return "no accuracy (not from the Dawarich app)"
    if not inside_trip_time:
        return "outside the trip's time"
    if meters is None:
        return f"more than {MAX_DISTANCE_FROM_ROUTE_METERS:.0f} m from the route"
    if accuracy > MAX_ACCURACY_METERS and abs(trainlog_time - timestamp) > INACCURATE_MAX_TIME_OFF_SECONDS:
        return (f"less accurate than {MAX_ACCURACY_METERS:.0f} m, and more than "
                f"{INACCURATE_MAX_TIME_OFF_SECONDS // 60:.0f} minutes off trainlog's times")
    return "too fast from or to the other phone points, or going back along the route"


def contradicted_stops(timestamp, meters, stops):
    """The realtime stop times a fix can't be in order with (as combine_anchors decides it): the phone
    was still before the stop after trainlog's minute of arriving there, or already past it
    before trainlog's minute of leaving it."""
    contradicted = []
    for stop in stops:
        if not stop["realtime"]:
            continue
        if meters < stop["meters"] and math.ceil(timestamp) >= math.floor(stop["arrival"] + ANCHOR_TIME_WINDOW_SECONDS):
            contradicted.append({"name": stop["name"], "meters_away": stop["meters"] - meters,
                                 "side": "before", "time": stop["arrival"]})
        elif meters > stop["meters"] and math.ceil(stop["departure"]) >= math.floor(timestamp):
            contradicted.append({"name": stop["name"], "meters_away": meters - stop["meters"],
                                 "side": "past", "time": stop["departure"]})
    return contradicted


def position_at(route, distance_along_route, meters):
    """[longitude, latitude] of the place this many meters along the route."""
    index = min(max(bisect.bisect_right(distance_along_route, meters), 1), len(route) - 1)
    before, after = distance_along_route[index - 1], distance_along_route[index]
    share = 0.0 if after == before else min(1.0, max(0.0, (meters - before) / (after - before)))
    return [route[index - 1][0] + share * (route[index][0] - route[index - 1][0]),
            route[index - 1][1] + share * (route[index][1] - route[index - 1][1])]


def waiting_at_a_stop(basis, timestamp, meters, accuracy):
    """Was the fix recorded near a stop between the ride's arrival and departure there?"""
    return any(
        abs(meters - basis.distance_along_route[stay.index]) <= STATION_RADIUS_METERS + accuracy
        and stay.arrival <= timestamp <= stay.departure + ANCHOR_TIME_WINDOW_SECONDS
        for stay in basis.stays
    )


def timeline_with(basis, exact_anchors):
    """(The trip's timeline with these exact anchors ([[timestamp, meters along the route], ...]),
    the timestamps of those left out for contradicting a realtime stop time)."""
    new_route, new_distances, exact, new_index_of_vertex = insert_exact_anchor_vertices(
        basis.route, basis.distance_along_route, exact_anchors)
    stops = [stop._replace(index=new_index_of_vertex[stop.index]) for stop in basis.stops]
    start = Anchor(0, basis.start, ENDPOINT)
    end = Anchor(len(new_route) - 1, basis.end, ENDPOINT)
    kept_stops, kept_exact, _ = combine_anchors(start, end, stops, exact, new_distances)
    kept_times = {anchor.time for anchor in kept_exact}
    left_out = {timestamp for timestamp, _ in exact_anchors if timestamp not in kept_times}
    return timeline(start, end, kept_stops, kept_exact, new_distances), left_out


def timeline(start, end, stops, exact, distance_along_route):
    """[(meters along the route, likely time there)] of the anchors, in route order."""
    anchors = sorted([start, end] + stops + exact, key=lambda anchor: anchor.index)
    return [(distance_along_route[anchor.index], likely_time(anchor)) for anchor in anchors]


def time_at(times, meters):
    """The time at a place along the route, spread by distance between the timeline's anchors."""
    for (meters_before, time_before), (meters_after, time_after) in zip(times, times[1:]):
        if meters <= meters_after:
            if meters_after == meters_before:
                return time_after
            share = max(0.0, (meters - meters_before) / (meters_after - meters_before))
            return time_before + share * (time_after - time_before)
    return times[-1][1]


def meters_at(times, timestamp):
    """Where along the route the timeline has the ride at a time."""
    for (meters_before, time_before), (meters_after, time_after) in zip(times, times[1:]):
        if timestamp <= time_after:
            if time_after <= time_before:
                return meters_after
            share = max(0.0, (timestamp - time_before) / (time_after - time_before))
            return meters_before + share * (meters_after - meters_before)
    return times[-1][0]


def plausible_chain(candidates, start_time, end_time, total_meters, max_speed):
    """The largest set of candidates (in time order) that goes along the route at plausible speeds,
    from the trip's start to its end."""
    nodes = [Candidate(start_time, 0.0, 0.0)] + candidates + [Candidate(end_time, total_meters, 0.0)]

    def plausible(before, after):
        if after.timestamp <= before.timestamp:
            return False
        if after.meters < before.meters - BACKTRACK_TOLERANCE_METERS - 2 * max(before.accuracy, after.accuracy):
            return False
        return max(0.0, after.meters - before.meters) / (after.timestamp - before.timestamp) <= max_speed

    longest = [1] + [0] * (len(nodes) - 1)  # most nodes on a plausible chain from the start to each node
    previous = [None] * len(nodes)
    for index in range(1, len(nodes)):
        for before in range(index):
            if longest[before] and longest[before] + 1 > longest[index] and plausible(nodes[before], nodes[index]):
                longest[index], previous[index] = longest[before] + 1, before
    if not longest[-1]:
        return []
    chain, index = [], previous[-1]
    while index:
        chain.append(nodes[index])
        index = previous[index]
    return chain[::-1]


def spread_out(chain, total_meters):
    """The chain with strictly increasing places along the route (standing still, GPS noise),
    without fixes that would end up at the trip's end."""
    spread = []
    for candidate in chain:
        meters = max(candidate.meters, MIN_METERS_BETWEEN_ANCHORS,
                     spread[-1].meters + MIN_METERS_BETWEEN_ANCHORS if spread else 0)
        if meters < total_meters - MIN_METERS_BETWEEN_ANCHORS:
            spread.append(candidate._replace(meters=meters))
    return spread


class RouteIndex:
    """Finds where on the route a position is, quickly: route segments are sorted into grid cells."""

    CELL_DEGREES = 0.01  # about 1 km
    MAX_CELLS_PER_SEGMENT = 400  # longer segments are checked for every position

    def __init__(self, route, distance_along_route):
        self.route = route
        self.distance_along_route = distance_along_route
        self.cells = {}
        self.long_segments = []
        margin_latitude = MAX_DISTANCE_FROM_ROUTE_METERS / 111_000
        for index, (a, b) in enumerate(zip(route, route[1:])):
            margin_longitude = margin_latitude / max(0.01, math.cos(math.radians(a[1])))
            low_x, high_x = self.cell(min(a[0], b[0]) - margin_longitude), self.cell(max(a[0], b[0]) + margin_longitude)
            low_y, high_y = self.cell(min(a[1], b[1]) - margin_latitude), self.cell(max(a[1], b[1]) + margin_latitude)
            if (high_x - low_x + 1) * (high_y - low_y + 1) > self.MAX_CELLS_PER_SEGMENT:
                self.long_segments.append(index)
                continue
            for x in range(low_x, high_x + 1):
                for y in range(low_y, high_y + 1):
                    self.cells.setdefault((x, y), []).append(index)

    def cell(self, degrees):
        return math.floor(degrees / self.CELL_DEGREES)

    def meters_along(self, position, expected_meters):
        """Meters along the route of the place closest to `position`, or None if that's more than
        MAX_DISTANCE_FROM_ROUTE_METERS away. Where the route passes the place more than once (there and
        back on the same road), the pass closest to `expected_meters` counts. Segments next to each
        other along the route are one pass (at a corner, both are near a position)."""
        near = []  # (meters at the segment's start, at its end, distance, meters of the closest place)
        for index in self.cells.get((self.cell(position[0]), self.cell(position[1])), []) + self.long_segments:
            distance, share = project_on_segment(position, self.route[index], self.route[index + 1])
            if distance <= MAX_DISTANCE_FROM_ROUTE_METERS:
                start, end = self.distance_along_route[index], self.distance_along_route[index + 1]
                near.append((start, end, distance, start + share * (end - start)))
        if not near:
            return None

        passes = []  # the closest place of each pass: (distance, meters)
        pass_end = None
        for start, end, distance, meters in sorted(near):
            if pass_end is None or start > pass_end + 2 * MAX_DISTANCE_FROM_ROUTE_METERS:
                passes.append((distance, meters))
            else:
                passes[-1] = min(passes[-1], (distance, meters))
            pass_end = end if pass_end is None else max(pass_end, end)
        return min(passes, key=lambda closest: (abs(closest[1] - expected_meters), closest[0]))[1]


def project_on_segment(position, segment_start, segment_end):
    """(distance in meters, share along the segment from 0 to 1) of the closest place on a straight segment."""
    reference_latitude = math.radians(position[1])

    def to_meters(other):
        longitude_difference = (other[0] - position[0] + 180) % 360 - 180
        return (6_371_000 * math.radians(longitude_difference) * math.cos(reference_latitude),
                6_371_000 * math.radians(other[1] - position[1]))

    start_x, start_y = to_meters(segment_start)
    end_x, end_y = to_meters(segment_end)
    segment_x, segment_y = end_x - start_x, end_y - start_y
    length_squared = segment_x ** 2 + segment_y ** 2
    share = 0.0 if not length_squared else max(0.0, min(1.0, -(start_x * segment_x + start_y * segment_y) / length_squared))
    return math.hypot(start_x + share * segment_x, start_y + share * segment_y), share

