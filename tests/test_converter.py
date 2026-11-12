import json
import unittest
from datetime import datetime, timezone

from trackfixer.converter import convert_trip, distance_meters

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


def s_bahn_trip_row(bellevue_time="2026-11-12 09:01:00", tiergarten_time="2026-11-12 09:01:00"):
    waypoints = [
        {"lat": STATIONS[name][1], "lng": STATIONS[name][0], "stop": {"arr_rt": time, "dep_rt": time}}
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


if __name__ == "__main__":
    unittest.main()
