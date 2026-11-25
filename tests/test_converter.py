import json
import unittest
from datetime import datetime, timezone

from trackfixer.converter import convert_trip, decode_path, distance_meters

from .helpers import encode_path

# An S-Bahn ride through Berlin, modelled on a real trainlog trip (times are UTC):
#   Friedrichstraße 08:54 -> Hauptbahnhof 08:57 -> Bellevue 09:01 -> Tiergarten 09:01
# Bellevue and Tiergarten share a minute, although the two stations are about 1 km apart.
STATIONS = {
    "Friedrichstraße": [13.38698, 52.52030],
    "Hauptbahnhof": [13.36960, 52.52510],
    "Bellevue": [13.34720, 52.51990],
    "Tiergarten": [13.33660, 52.51435],
}


def dense_route(corners, step_meters=20):
    """The corners joined by straight lines with a vertex every few meters, like a real route."""
    route = [corners[0]]
    for start, end in zip(corners, corners[1:]):
        steps = max(1, round(distance_meters(start, end) / step_meters))
        for step in range(1, steps + 1):
            route.append([start[0] + (end[0] - start[0]) * step / steps,
                          start[1] + (end[1] - start[1]) * step / steps])
    return route


def s_bahn_trip_row(bellevue_time="2026-11-12 09:01:00", tiergarten_time="2026-11-12 09:01:00", realtime=True):
    keys = ("arr_rt", "dep_rt") if realtime else ("arr", "dep")
    waypoints = [
        {"lat": STATIONS[name][1], "lng": STATIONS[name][0], "stop": {keys[0]: time, keys[1]: time}}
        for name, time in [("Hauptbahnhof", "2026-11-12 08:57:00"), ("Bellevue", bellevue_time)]
    ]
    return {
        "uid": "1033414",
        "type": "train",
        "utc_start_datetime": "2026-11-12 08:54:00",
        "utc_end_datetime": tiergarten_time,
        "departure_delay": "0",
        "arrival_delay": "0",
        "waypoints": json.dumps(waypoints),
        "path": encode_path(dense_route(list(STATIONS.values()))),
    }


def time_at(route, station):
    nearest = min(route, key=lambda point: distance_meters([point["longitude"], point["latitude"]],
                                                           STATIONS[station]))
    return nearest["timestamp"]


def waiting_at_hauptbahnhof(row, departure="2026-11-12 09:00:00"):
    """The row with the S-Bahn leaving Hauptbahnhof only at `departure` (it arrived at 08:57)."""
    waypoints = json.loads(row["waypoints"])
    waypoints[0]["stop"]["dep_rt"] = departure
    return dict(row, waypoints=json.dumps(waypoints))


def position_at(row, meters):
    """[longitude, latitude] some meters along the trip's route."""
    route = decode_path(row["path"])
    for a, b in zip(route, route[1:]):
        length = distance_meters(a, b)
        if meters <= length:
            share = meters / length
            return [a[0] + share * (b[0] - a[0]), a[1] + share * (b[1] - a[1])]
        meters -= length
    return route[-1]


def meters_before(station, row, meters_earlier=0):
    """Meters along the trip's route to a station (or some meters before it)."""
    route = decode_path(row["path"])
    nearest = min(range(len(route)), key=lambda index: distance_meters(route[index], STATIONS[station]))
    return sum(distance_meters(a, b) for a, b in zip(route[:nearest], route[1:nearest + 1])) - meters_earlier


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class ConverterTest(unittest.TestCase):
    def test_no_two_points_share_a_second(self):
        route, _ = convert_trip(s_bahn_trip_row())
        timestamps = [point["timestamp"] for point in route]
        self.assertEqual(timestamps, sorted(set(timestamps)))

    def test_two_stops_in_the_same_minute_are_spread_over_that_minute(self):
        route, _ = convert_trip(s_bahn_trip_row())
        bellevue, tiergarten = time_at(route, "Bellevue"), time_at(route, "Tiergarten")

        # Both stations stay inside the 09:01 minute, but far enough apart for a plausible speed.
        self.assertGreaterEqual(bellevue, utc("2026-11-12 09:01:00"))
        self.assertLessEqual(tiergarten, utc("2026-11-12 09:01:59"))
        meters_per_second = distance_meters(STATIONS["Bellevue"], STATIONS["Tiergarten"]) / (tiergarten - bellevue)
        self.assertLess(meters_per_second * 3.6, 80)

    def test_a_stop_one_minute_before_the_end_keeps_a_plausible_speed(self):
        route, _ = convert_trip(s_bahn_trip_row(tiergarten_time="2026-11-12 09:02:00"))
        bellevue, tiergarten = time_at(route, "Bellevue"), time_at(route, "Tiergarten")

        meters_per_second = distance_meters(STATIONS["Bellevue"], STATIONS["Tiergarten"]) / (tiergarten - bellevue)
        self.assertLess(meters_per_second * 3.6, 80)

    def test_a_trip_without_stops_keeps_its_exact_times(self):
        row = s_bahn_trip_row(tiergarten_time="2026-11-12 09:02:00")
        row["waypoints"] = "[]"
        route, warnings = convert_trip(row)

        self.assertEqual(route[0]["timestamp"], utc("2026-11-12 08:54:00"))
        self.assertEqual(route[-1]["timestamp"], utc("2026-11-12 09:02:00"))
        self.assertEqual(warnings, [])


    def test_the_train_waits_at_a_stop_from_arrival_to_departure(self):
        row = waiting_at_hauptbahnhof(s_bahn_trip_row(tiergarten_time="2026-11-12 09:05:00"))
        route, warnings = convert_trip(row)

        at_hauptbahnhof = [point["timestamp"] for point in route
                           if distance_meters([point["longitude"], point["latitude"]], STATIONS["Hauptbahnhof"]) < 30]
        self.assertEqual(len(at_hauptbahnhof), 2)
        arrival, departure = at_hauptbahnhof
        self.assertTrue(utc("2026-11-12 08:57:00") <= arrival <= utc("2026-11-12 08:57:59"))
        self.assertTrue(utc("2026-11-12 09:00:00") <= departure <= utc("2026-11-12 09:00:59"))
        self.assertFalse([warning for warning in warnings if "Inconsistent" in warning])

    def test_without_exact_anchors_nothing_changes(self):
        self.assertEqual(convert_trip(s_bahn_trip_row()), convert_trip(s_bahn_trip_row(), exact_anchors=[]))

    def test_an_exact_anchor_keeps_its_second_even_where_the_train_was_fast(self):
        # Stood still at Hauptbahnhof until 08:59:10, then 2 km to Bellevue by 09:01:
        # faster than twice the average speed, which minute-precise stops may not be.
        row = s_bahn_trip_row(tiergarten_time="2026-11-12 09:03:00")
        leaving = utc("2026-11-12 08:59:10")
        route, warnings = convert_trip(row, exact_anchors=[[leaving, meters_before("Hauptbahnhof", row) + 1]])

        self.assertIn(leaving, [point["timestamp"] for point in route])
        self.assertLess(time_at(route, "Bellevue"), utc("2026-11-12 09:02:00"))
        self.assertEqual([warning for warning in warnings if "phone" in warning or "Inconsistent" in warning], [])

    def test_a_realtime_stop_time_wins_over_a_contradicting_phone_point(self):
        # The phone point has the train past Hauptbahnhof before trainlog's realtime time there.
        row = s_bahn_trip_row()
        too_early = utc("2026-11-12 08:55:30")
        route, warnings = convert_trip(row, exact_anchors=[[too_early, meters_before("Hauptbahnhof", row) + 200]])

        self.assertNotIn(too_early, [point["timestamp"] for point in route])
        self.assertGreaterEqual(time_at(route, "Hauptbahnhof"), utc("2026-11-12 08:57:00"))
        self.assertIn("1 phone point(s) contradicted trainlog's realtime stop times and weren't used", warnings)

    def test_a_scheduled_stop_time_contradicted_by_the_phone_is_left_out(self):
        # The S-Bahn ran 3 minutes late, which trainlog's scheduled times don't know.
        row = s_bahn_trip_row(tiergarten_time="2026-11-12 09:06:00", realtime=False)
        near_bellevue = utc("2026-11-12 09:04:00")
        route, warnings = convert_trip(row, exact_anchors=[[near_bellevue, meters_before("Bellevue", row, 50)]])

        self.assertGreaterEqual(time_at(route, "Bellevue"), near_bellevue)  # not at 09:01
        self.assertIn("1 scheduled stop time(s) contradicted the phone's points and weren't used", warnings)


if __name__ == "__main__":
    unittest.main()
