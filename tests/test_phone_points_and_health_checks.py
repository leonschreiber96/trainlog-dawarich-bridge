import unittest

from trackfixer.converter import convert_trip
from trackfixer.health_checks import check_phone_tracker_ids, check_shortcut_heartbeat
from trackfixer.phone_points import phone_points_during_trip, trip_time_looks_wrong

from .helpers import (APP_TRACKER_ID, encode_path, example_bus_trip_row, example_day_points,
                      meters_to_latitude_longitude, position_along_bus_route)


def berlin_time(timestamp):
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=1))).strftime("%H:%M:%S")


class PhonePointsDuringTripTest(unittest.TestCase):
    def test_finds_the_app_points_of_the_bus_ride_but_keeps_the_walk_to_the_stop(self):
        route, _ = convert_trip(example_bus_trip_row())
        found = phone_points_during_trip(route, example_day_points())

        # 12:25:33 (getting off at the destination stop, just after trainlog's arrival time)
        # is kept: the walk from the stop starts there.
        self.assertEqual(
            [berlin_time(point["timestamp"]) for point in found],
            ["12:16:41", "12:17:41", "12:19:48", "12:20:52", "12:21:56", "12:23:04", "12:24:15"],
        )
        self.assertFalse(trip_time_looks_wrong(route, found))

    def test_points_on_the_route_just_outside_the_trip_time_are_only_kept_at_the_station(self):
        # Just before departure and just after arrival, both on the route.
        route, _ = convert_trip(example_bus_trip_row())
        trip_start, trip_end = route[0]["timestamp"], route[-1]["timestamp"]

        def phone_point(timestamp, position):
            latitude, longitude = meters_to_latitude_longitude(position)
            return {"timestamp": timestamp, "latitude": str(latitude), "longitude": str(longitude),
                    "tracker_id": APP_TRACKER_ID}

        waiting_at_the_stop = phone_point(trip_start - 60, position_along_bus_route(0.02))  # ~40 m from the stop
        getting_off = phone_point(trip_end + 60, position_along_bus_route(0.98))  # ~40 m from the stop
        still_on_the_bus = phone_point(trip_end + 60, position_along_bus_route(0.85))  # ~270 m before the stop
        found = phone_points_during_trip(route, [waiting_at_the_stop, getting_off, still_on_the_bus])
        self.assertEqual(found, [still_on_the_bus])

    def test_a_trip_logged_at_the_wrong_time_is_recognized(self):
        # A trip at the same time as the bus ride, but about 5 km away: the phone was
        # somewhere else, so this trip's time in trainlog must be wrong.
        bus_route, _ = convert_trip(example_bus_trip_row())
        route_5_km_north = [[point["longitude"], point["latitude"] + 0.045] for point in bus_route]
        route, _ = convert_trip(example_bus_trip_row(path=encode_path(route_5_km_north)))
        found = phone_points_during_trip(route, example_day_points())
        self.assertTrue(trip_time_looks_wrong(route, found))


class HealthCheckTest(unittest.TestCase):
    def test_warns_when_the_shortcut_sends_no_tracker_id(self):
        warnings = check_phone_tracker_ids(example_day_points())
        self.assertEqual(len(warnings), 1)
        self.assertIn("without a tracker id", warnings[0])

    def test_no_warning_when_all_phone_points_share_one_tracker_id(self):
        points = [dict(point, tracker_id=APP_TRACKER_ID) if not point["tracker_id"] else point
                  for point in example_day_points()]
        self.assertEqual(check_phone_tracker_ids(points), [])

    def test_no_warning_once_the_shortcut_was_fixed_during_the_day(self):
        # Shortcut points before 13:00 still have no tracker id, later ones have the app's.
        one_pm = example_day_points()[0]["timestamp"] + 13 * 60 * 60
        points = [dict(point, tracker_id=APP_TRACKER_ID) if not point["tracker_id"] and point["timestamp"] > one_pm
                  else point for point in example_day_points()]
        self.assertEqual(check_phone_tracker_ids(points), [])

    def test_warns_when_the_app_got_a_new_tracker_id(self):
        # E.g. after reinstalling the app: the Shortcut still sends the old id.
        points = [dict(point, tracker_id=APP_TRACKER_ID) if not point["tracker_id"] else point
                  for point in example_day_points()]
        points = [dict(point, tracker_id="NEW-APP-ID") if point["tracker_id"] == APP_TRACKER_ID
                  and point["accuracy"] is not None else point for point in points]
        warnings = check_phone_tracker_ids(points)
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"sends tracker id {APP_TRACKER_ID}, but the Dawarich app now uses NEW-APP-ID", warnings[0])

    def test_shortcut_heartbeat(self):
        points = example_day_points()
        day_start, day_end = points[0]["timestamp"], points[-1]["timestamp"] + 60
        # The longest silence that day was 30 minutes (15:15 to 15:45).
        self.assertEqual(check_shortcut_heartbeat(points, day_start, day_end, max_silence_minutes=60), [])
        self.assertEqual(len(check_shortcut_heartbeat(points, day_start, day_end, max_silence_minutes=20)), 1)
        self.assertEqual(len(check_shortcut_heartbeat([], day_start, day_end, max_silence_minutes=60)), 1)


if __name__ == "__main__":
    unittest.main()
