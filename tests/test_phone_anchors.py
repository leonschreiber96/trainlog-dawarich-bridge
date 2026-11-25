import math
import unittest
from datetime import datetime, timezone

from trackfixer.converter import convert_trip, distance_meters
from trackfixer.phone_anchors import (AT_A_STOP, CONTRADICTS_STOP, IGNORED, NOT_NEEDED, UNUSABLE, USED,
                                      assess_phone_fixes, choose_exact_anchors, fixes_from_points)

from .helpers import APP_TRACKER_ID, encode_path, meters_to_latitude_longitude
from .test_converter import meters_before, position_at, s_bahn_trip_row, utc, waiting_at_hauptbahnhof

# A car ride with a traffic jam, modelled on a real one (made-up place and times, UTC):
#   13:00-13:15  20 km at 80 km/h
#   13:15-13:35  3 km in a traffic jam
#   13:35-13:41  the last 7.5 km at 75 km/h
# The phone sent an accurate point every minute, a few meters off the road.
START = int(datetime(2026, 11, 24, 13, 0, tzinfo=timezone.utc).timestamp())
END = START + 41 * 60
ROAD_CORNERS = [(0, 0), (0, 12_000), (3_000, 20_000), (3_000, 30_000)]  # meters east, north
ROAD_METERS = sum(math.dist(start, end) for start, end in zip(ROAD_CORNERS, ROAD_CORNERS[1:]))  # 30.5 km


def utc_text(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def longitude_latitude(position):
    latitude, longitude = meters_to_latitude_longitude(position)
    return [longitude, latitude]


def position_along(corners, meters):
    """(meters east, meters north) after some meters along the corners."""
    for start, end in zip(corners, corners[1:]):
        length = math.dist(start, end)
        if meters <= length:
            share = meters / length
            return (start[0] + share * (end[0] - start[0]), start[1] + share * (end[1] - start[1]))
        meters -= length
    return corners[-1]


def road_route(corners, step_meters=250):
    length = sum(math.dist(start, end) for start, end in zip(corners, corners[1:]))
    steps = math.ceil(length / step_meters)
    return [longitude_latitude(position_along(corners, length * step / steps)) for step in range(steps + 1)]


def meters_driven(timestamp):
    """Where the car really was."""
    minutes = (timestamp - START) / 60
    if minutes <= 15:
        return minutes / 15 * 20_000
    if minutes <= 35:
        return 20_000 + (minutes - 15) / 20 * 3_000
    return min(ROAD_METERS, 23_000 + (minutes - 35) / 6 * (ROAD_METERS - 23_000))


def car_row(corners=ROAD_CORNERS, start=START, end=END, **changes):
    row = {"uid": "drawn-1", "type": "car", "path": encode_path(road_route(corners)),
           "utc_start_datetime": utc_text(start), "utc_end_datetime": utc_text(end)}
    row.update(changes)
    return row


def fix(timestamp, position, accuracy=10):
    latitude, longitude = meters_to_latitude_longitude(position)
    return {"timestamp": timestamp, "latitude": latitude, "longitude": longitude, "accuracy": accuracy}


def fixes_of_the_ride(meters_at=meters_driven, corners=ROAD_CORNERS, start=START, end=END):
    fixes = []
    for minute, timestamp in enumerate(range(start + 30, end, 60)):
        east, north = position_along(corners, meters_at(timestamp))
        offset = 6 if minute % 2 else -4  # GPS noise
        fixes.append(fix(timestamp, (east + offset, north - offset)))
    return fixes


def seconds_late(route, meters):
    """How much later than really the converted route has the car after some meters (negative: earlier).
    Between two of the route's points, time is spread by distance, as Dawarich shows it."""
    driven = 0.0
    for point, next_point in zip(route, route[1:]):
        length = distance_meters([point["longitude"], point["latitude"]], [next_point["longitude"], next_point["latitude"]])
        if driven + length >= meters:
            time_there = point["timestamp"] + (meters - driven) / length * (next_point["timestamp"] - point["timestamp"])
            break
        driven += length
    really = next(timestamp for timestamp in range(START, END + 1) if meters_driven(timestamp) >= meters)
    return time_there - really


class PhoneAnchorTest(unittest.TestCase):
    def test_a_traffic_jam_is_recorded(self):
        row = car_row()
        anchors = choose_exact_anchors(row, fixes_of_the_ride())
        without_anchors, _ = convert_trip(row, 2)
        with_anchors, warnings = convert_trip(row, 2, anchors)

        self.assertLessEqual(len(anchors), 6)  # only where it matters: when the jam started and ended
        self.assertEqual(warnings, [])
        # In the middle of the jam, evenly spread time has the car there 3 minutes late...
        self.assertGreaterEqual(seconds_late(without_anchors, 21_500), 3 * 60)
        # ...the phone's points put it right, within about a minute everywhere.
        for meters in range(1_000, 30_000, 1_000):
            with self.subTest(meters=meters):
                self.assertLess(abs(seconds_late(with_anchors, meters)), 65)

    def test_exact_anchors_keep_their_second(self):
        row = car_row()
        anchors = choose_exact_anchors(row, fixes_of_the_ride())
        route, _ = convert_trip(row, 2, anchors)
        self.assertTrue({timestamp for timestamp, _ in anchors} <= {point["timestamp"] for point in route})

    def test_a_steady_ride_needs_no_anchors(self):
        def steady(timestamp):
            return (timestamp - START) / (END - START) * ROAD_METERS

        anchors, statuses = assess_phone_fixes(car_row(), fixes_of_the_ride(steady))
        self.assertEqual(anchors, [])
        self.assertEqual(set(statuses.values()), {NOT_NEEDED})

    def test_points_off_the_road_or_at_an_impossible_place_are_not_used(self):
        fixes = fixes_of_the_ride()
        old_position = fix(START + 16 * 60 + 10, position_along(ROAD_CORNERS, 2_000))  # 18 km back, a minute later
        off_the_road = fix(START + 16 * 60 + 20, (2_000, 15_000))
        _, statuses = assess_phone_fixes(car_row(), fixes + [old_position, off_the_road])

        for unusable in (old_position, off_the_road):
            self.assertEqual(statuses[unusable["timestamp"]], UNUSABLE)
        self.assertIn(USED, statuses.values())

    def test_points_the_phone_calls_inaccurate_time_the_ride_if_they_are_on_the_route(self):
        # Phones in trains often report kilometers of accuracy for points right on the tracks.
        row = car_row()
        fixes = [dict(fix, accuracy=2000) for fix in fixes_of_the_ride()]
        anchors = choose_exact_anchors(row, fixes)
        route, _ = convert_trip(row, 2, anchors)

        self.assertTrue(anchors)
        for meters in range(1_000, 30_000, 1_000):
            with self.subTest(meters=meters):
                self.assertLess(abs(seconds_late(route, meters)), 65)

    def test_an_inaccurate_point_on_the_route_far_off_the_trips_times_is_not_used(self):
        # On the road, but where the evenly timed ride is 23 minutes later: a position off by kilometers
        # that happens to lie on the route. As accurate as the phone says, it would count.
        timestamp = START + 17 * 60
        position = position_along(ROAD_CORNERS, 30_000)
        _, statuses = assess_phone_fixes(car_row(), [fix(timestamp, position, accuracy=2000)])
        self.assertEqual(statuses[timestamp], UNUSABLE)
        _, statuses = assess_phone_fixes(car_row(), [fix(timestamp, position, accuracy=10)])
        self.assertNotEqual(statuses[timestamp], UNUSABLE)

    def test_fixes_left_out_on_the_map_are_not_used(self):
        fixes = fixes_of_the_ride()
        anchors = choose_exact_anchors(car_row(), fixes)
        left_out = [timestamp for timestamp, _ in anchors]
        anchors_now, statuses = assess_phone_fixes(car_row(ignored_phone_fixes=left_out), fixes)

        self.assertEqual({statuses[timestamp] for timestamp in left_out}, {IGNORED})
        self.assertFalse(set(left_out) & {timestamp for timestamp, _ in anchors_now})

    def test_a_route_that_passes_a_place_twice_uses_the_right_pass(self):
        # 5 km out, a 30 minute break, 5 km back, on the same road.
        corners = [(0, 0), (0, 5_000), (0, 0)]
        end = START + 90 * 60

        def walked(timestamp):
            minutes = (timestamp - START) / 60
            return min(minutes, 30) / 30 * 5_000 + max(0, minutes - 60) / 30 * 5_000

        row = car_row(corners, end=end, type="walk")
        anchors = choose_exact_anchors(row, fixes_of_the_ride(walked, corners, end=end))
        route, _ = convert_trip(row, 2, anchors)

        self.assertTrue(anchors)
        timestamps = [point["timestamp"] for point in route]
        self.assertEqual(timestamps, sorted(timestamps))
        halfway_back = position_along(corners, 7_500)
        nearest_on_the_way_back = min((point for point in route if point["timestamp"] > START + 45 * 60),
                                      key=lambda point: distance_meters([point["longitude"], point["latitude"]],
                                                                        longitude_latitude(halfway_back)))
        self.assertLess(abs(nearest_on_the_way_back["timestamp"] - (START + 75 * 60)), 90)

    def test_a_point_recorded_while_waiting_at_a_stop_is_not_a_contradiction(self):
        # Like a tram waiting 3 minutes at a stop: just before it left, the phone put it 20 m
        # before the stop's spot on the route (GPS noise, or a long platform).
        row = waiting_at_hauptbahnhof(s_bahn_trip_row(tiergarten_time="2026-11-12 09:05:00"))
        longitude, latitude = position_at(row, meters_before("Hauptbahnhof", row, 20))
        waiting = {"timestamp": int(utc("2026-11-12 09:00:27")), "latitude": latitude, "longitude": longitude,
                   "accuracy": 14}
        anchors, statuses = assess_phone_fixes(row, [waiting])

        self.assertEqual(statuses[waiting["timestamp"]], AT_A_STOP)
        self.assertEqual(anchors, [])

        # Without the departure time in trainlog, it's still the arrival's minute that counts:
        # 3 minutes later, the train should have been well past the stop.
        row_without_waiting = s_bahn_trip_row(tiergarten_time="2026-11-12 09:05:00")
        _, statuses = assess_phone_fixes(row_without_waiting, [waiting])
        self.assertEqual(statuses[waiting["timestamp"]], CONTRADICTS_STOP)

    def test_app_points_during_the_trip_become_phone_fixes_however_accurate(self):
        def point(timestamp, **properties):
            latitude, longitude = meters_to_latitude_longitude((0, 100))
            return dict({"timestamp": timestamp, "latitude": str(latitude), "longitude": str(longitude),
                         "tracker_id": APP_TRACKER_ID, "accuracy": 10}, **properties)

        points = [
            point(START + 60),
            point(START + 120, accuracy=None, battery=80),  # the Shortcut
            point(START + 180, accuracy=2000),
            point(START + 240, anomaly=True),  # Dawarich flags inaccurate points as GPS noise
            point(START + 300, tracker_id="trainlog-99"),  # another trip
            point(START - 60),  # before the trip
        ]
        self.assertEqual([fix["timestamp"] for fix in fixes_from_points(points, START, END)],
                         [START + 60, START + 180, START + 240])


if __name__ == "__main__":
    unittest.main()
