import math
import unittest
import unittest.mock
from datetime import datetime

from trackfixer import database
from trackfixer.converter import convert_trip, distance_meters
from trackfixer.phone_point_cleanup import remove_phone_points_during_trips
from trackfixer.run_log import RunLog
from trackfixer.trip_sync import CONVERTER_VERSION, sync_trips, trip_fingerprint

from .helpers import (APP_TRACKER_ID, BERLIN, FakeDawarich, example_bus_trip_row, example_day_points,
                      meters_to_latitude_longitude, position_along_bus_route, temporary_settings)


def new_run_log():
    return RunLog(database.start_run("manual"))


def points_of_tracker(dawarich, tracker_id):
    return [point for point in dawarich.points if point.get("tracker_id") == tracker_id]


class TripSyncTest(unittest.TestCase):
    def setUp(self):
        self.settings = temporary_settings()
        self.dawarich = FakeDawarich()

    def test_new_trip_is_sent_as_its_own_tracker(self):
        log = new_run_log()
        sync_trips([example_bus_trip_row()], True, self.dawarich, log)

        expected_route, _ = convert_trip(example_bus_trip_row())
        self.assertEqual(log.trips_sent, 1)
        self.assertEqual(len(points_of_tracker(self.dawarich, "trainlog-1234")), len(expected_route))
        self.assertEqual(database.get_trip("1234")["status"], database.IN_DAWARICH)

    def test_points_tell_dawarich_the_mode_of_transport(self):
        sync_trips([example_bus_trip_row(), example_bus_trip_row(uid="2", type="walk", utc_start_datetime="2026-11-09 11:16:00",
                                                                 utc_end_datetime="2026-11-09 11:25:00")],
                   True, self.dawarich, new_run_log())

        bus_hints = {str(point["motion_data"]) for point in points_of_tracker(self.dawarich, "trainlog-1234")}
        walk_hints = {str(point["motion_data"]) for point in points_of_tracker(self.dawarich, "trainlog-2")}
        self.assertEqual(bus_hints, {"{'activity': 'IN_BUS'}"})
        self.assertEqual(walk_hints, {"{'motion': ['walking'], 'motion_authoritative': True}"})

    def test_changing_the_trip_type_in_trainlog_sends_the_trip_again(self):
        sync_trips([example_bus_trip_row()], True, self.dawarich, new_run_log())
        log = new_run_log()
        sync_trips([example_bus_trip_row(type="tram")], True, self.dawarich, log)

        self.assertEqual(log.trips_sent, 1)
        tram_hints = {str(point["motion_data"]) for point in points_of_tracker(self.dawarich, "trainlog-1234")}
        self.assertEqual(tram_hints, {"{'activity': 'IN_TRAM'}"})

    def test_unchanged_trip_is_not_sent_again(self):
        sync_trips([example_bus_trip_row()], True, self.dawarich, new_run_log())
        points_before = points_of_tracker(self.dawarich, "trainlog-1234")
        log = new_run_log()
        sync_trips([example_bus_trip_row(notes="edited a note")], True, self.dawarich, log)

        self.assertEqual(log.trips_sent, 0)
        self.assertEqual(points_of_tracker(self.dawarich, "trainlog-1234"), points_before)

    def test_changed_trip_replaces_its_points(self):
        sync_trips([example_bus_trip_row()], True, self.dawarich, new_run_log())
        old_points = points_of_tracker(self.dawarich, "trainlog-1234")

        log = new_run_log()
        sync_trips([example_bus_trip_row(arrival_delay="120")], True, self.dawarich, log)
        new_points = points_of_tracker(self.dawarich, "trainlog-1234")
        expected_route, _ = convert_trip(example_bus_trip_row(arrival_delay="120"))

        self.assertEqual(log.trips_sent, 1)
        self.assertEqual([point["timestamp"] for point in new_points], [point["timestamp"] for point in expected_route])
        self.assertEqual(new_points[-1]["timestamp"], old_points[-1]["timestamp"] + 120)

    def test_a_change_in_trainlog_names_the_changed_columns(self):
        sync_trips([example_bus_trip_row()], True, self.dawarich, new_run_log())
        log = new_run_log()
        sync_trips([example_bus_trip_row(arrival_delay="120")], True, self.dawarich, log)

        messages = [event["message"] for event in database.run_events(log.run_id)]
        self.assertTrue(any("changed in trainlog (arrival_delay)" in message for message in messages))

    def test_trips_sent_again_after_an_app_update_are_not_blamed_on_trainlog(self):
        sync_trips([example_bus_trip_row()], True, self.dawarich, new_run_log())
        log = new_run_log()
        with unittest.mock.patch("trackfixer.trip_sync.CONVERTER_VERSION", CONVERTER_VERSION + 1):
            sync_trips([example_bus_trip_row()], True, self.dawarich, log)

        messages = [event["message"] for event in database.run_events(log.run_id)]
        self.assertEqual(log.trips_sent, 1)
        self.assertFalse(any("changed in trainlog" in message for message in messages))
        self.assertTrue(any("1 of them didn't change in trainlog" in message for message in messages))

    def test_trip_deleted_in_trainlog_is_removed_from_dawarich(self):
        sync_trips([example_bus_trip_row(), example_bus_trip_row(uid="99", utc_start_datetime="2026-11-09 11:16:00",
                                                                 utc_end_datetime="2026-11-09 11:25:00")],
                   True, self.dawarich, new_run_log())
        log = new_run_log()
        sync_trips([example_bus_trip_row(uid="99", utc_start_datetime="2026-11-09 11:16:00",
                                         utc_end_datetime="2026-11-09 11:25:00")], True, self.dawarich, log)

        self.assertEqual(log.trips_removed, 1)
        self.assertEqual(points_of_tracker(self.dawarich, "trainlog-1234"), [])
        self.assertEqual(database.get_trip("1234")["status"], database.REMOVED)

    def test_trips_missing_from_a_partial_upload_are_kept(self):
        sync_trips([example_bus_trip_row()], True, self.dawarich, new_run_log())
        sync_trips([], False, self.dawarich, new_run_log())
        self.assertEqual(database.get_trip("1234")["status"], database.IN_DAWARICH)

    def test_places_planned_trips_and_broken_trips_are_not_sent(self):
        log = new_run_log()
        sync_trips([
            example_bus_trip_row(uid="1", type="restaurant"),
            example_bus_trip_row(uid="2", utc_start_datetime="2099-01-01 10:00:00", utc_end_datetime="2099-01-01 11:00:00"),
            example_bus_trip_row(uid="3", path=""),
        ], True, self.dawarich, log)

        self.assertEqual(self.dawarich.points, [])
        self.assertEqual(database.get_trip("1")["status"], database.SKIPPED)
        self.assertEqual(database.get_trip("2")["status"], database.PLANNED)
        self.assertEqual(database.get_trip("3")["status"], database.FAILED)
        self.assertEqual(len(log.problems), 1)  # only the broken trip is a problem


class PhonePointCleanupTest(unittest.TestCase):
    def test_deletes_the_phone_points_of_the_bus_ride_and_backs_them_up(self):
        settings = temporary_settings()
        dawarich = FakeDawarich(example_day_points())
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())

        log = new_run_log()
        remove_phone_points_during_trips(dawarich, settings, log)

        self.assertEqual(log.phone_points_deleted, 7)  # the point getting off at the stop is kept
        self.assertEqual(database.get_trip("1234")["phone_points_deleted"], 7)
        self.assertEqual(len(list(settings.deleted_points_directory.glob("*.json"))), 1)
        # The old trainlog import of the example day is still there, which is reported.
        self.assertIn("old trainlog imports", log.problems[0][1])

        second_log = new_run_log()
        remove_phone_points_during_trips(dawarich, settings, second_log)
        self.assertEqual(second_log.phone_points_deleted, 0)

    def test_deletes_nothing_above_the_safety_limit(self):
        settings = temporary_settings(max_phone_points_deleted_per_run=5)
        dawarich = FakeDawarich(example_day_points())
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())

        log = new_run_log()
        remove_phone_points_during_trips(dawarich, settings, log)

        self.assertEqual(log.phone_points_deleted, 0)
        expected_route, _ = convert_trip(example_bus_trip_row())
        self.assertEqual(len(dawarich.points), len(example_day_points()) + len(expected_route))
        self.assertTrue(log.has_errors)

    def test_sends_a_trip_dawarich_made_no_track_from_again_once_then_warns(self):
        settings = temporary_settings()
        dawarich = FakeDawarich(example_day_points())
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())
        remove_phone_points_during_trips(dawarich, settings, new_run_log())
        for point in dawarich.points:
            point["track_id"] = None if point["tracker_id"] == "trainlog-1234" else 1

        def trip_point_ids():
            return [point["id"] for point in points_of_tracker(dawarich, "trainlog-1234")]

        def track_warnings(log):
            return [message for _, message in log.problems if "no track" in message]

        def an_hour_passes():
            with database.connect() as connection:
                connection.execute("UPDATE trips SET sent_at = '2026-11-10T12:00:00+00:00'")

        # Right after sending, Dawarich is still building tracks.
        ids_as_sent = trip_point_ids()
        just_sent_log = new_run_log()
        remove_phone_points_during_trips(dawarich, settings, just_sent_log)
        self.assertEqual(trip_point_ids(), ids_as_sent)
        self.assertEqual(track_warnings(just_sent_log), [])

        # Later, the trip still has no track: its points are sent again.
        an_hour_passes()
        resending_log = new_run_log()
        remove_phone_points_during_trips(dawarich, settings, resending_log)
        self.assertNotEqual(trip_point_ids(), ids_as_sent)
        self.assertEqual(len(trip_point_ids()), len(ids_as_sent))
        self.assertIsNotNone(database.get_trip("1234")["resent_for_track_at"])
        self.assertEqual(track_warnings(resending_log), [])

        # Still no track after that: only a warning, no endless resending.
        an_hour_passes()
        resent_ids = trip_point_ids()
        warning_log = new_run_log()
        remove_phone_points_during_trips(dawarich, settings, warning_log)
        self.assertEqual(trip_point_ids(), resent_ids)
        self.assertEqual(len(track_warnings(warning_log)), 1)
        self.assertIn("Sending its points again didn't help", track_warnings(warning_log)[0])


def berlin_timestamp(time_text):
    hours, minutes, seconds = (int(part) for part in time_text.split(":"))
    return int(datetime(2026, 11, 10, hours, minutes, seconds, tzinfo=BERLIN).timestamp())


def app_points_on_the_bus(progress_by_time, first_id=1000):
    """Accurate app points on the bus ride of the example day, at a share (0 to 1) of the route at each time."""
    points = []
    for index, (time_text, progress) in enumerate(progress_by_time):
        latitude, longitude = meters_to_latitude_longitude(position_along_bus_route(progress))
        points.append({"id": first_id + index, "timestamp": berlin_timestamp(time_text), "latitude": str(latitude),
                       "longitude": str(longitude), "tracker_id": APP_TRACKER_ID, "accuracy": 8})
    return points


# The bus stood still a quarter of the way for five minutes (12:17:30 to 12:22:30), then hurried on.
BUS_STOOD_STILL = [("12:17:00", 0.15), ("12:17:30", 0.25), ("12:18:30", 0.25), ("12:19:30", 0.25),
                   ("12:20:30", 0.25), ("12:21:30", 0.25), ("12:22:30", 0.25), ("12:23:30", 0.6), ("12:24:30", 0.95)]
# A steady ride: where the evenly spread time has the bus anyway.
BUS_WENT_STEADILY = [("12:17:00", 0.11), ("12:19:00", 0.33), ("12:21:00", 0.56), ("12:23:00", 0.78)]


def time_at_progress(points, progress):
    """When the trip's points have the bus at a share of the route: time spread by distance between them."""
    positions = [[float(point["longitude"]), float(point["latitude"])] for point in points]
    lengths = [distance_meters(a, b) for a, b in zip(positions, positions[1:])]
    remaining = progress * sum(lengths)
    for (point, next_point), length in zip(zip(points, points[1:]), lengths):
        if remaining <= length:
            return point["timestamp"] + remaining / length * (next_point["timestamp"] - point["timestamp"])
        remaining -= length
    return points[-1]["timestamp"]


class PhoneAnchorSyncTest(unittest.TestCase):
    """trainlog trips timed with the accurate phone points recorded during them."""

    def setUp(self):
        self.settings = temporary_settings()

    def trip_points(self, dawarich):
        return points_of_tracker(dawarich, "trainlog-1234")

    def assert_bus_stood_still(self, dawarich):
        points = self.trip_points(dawarich)
        # Evenly spread, the bus would be 60 % along at 12:21:24; it was there at 12:23:30.
        self.assertLess(abs(time_at_progress(points, 0.6) - berlin_timestamp("12:23:30")), 30)
        self.assertLess(abs(time_at_progress(points, 0.25) - berlin_timestamp("12:20:00")), 3 * 60)

    def test_a_bus_that_stood_still_is_timed_by_the_phone(self):
        dawarich = FakeDawarich(app_points_on_the_bus(BUS_STOOD_STILL))
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())

        self.assert_bus_stood_still(dawarich)
        self.assertIn("Timed with", database.get_trip("1234")["status_detail"])
        self.assertEqual(len(database.phone_fixes("1234")), len(BUS_STOOD_STILL))

    def test_the_timing_survives_deleting_the_phone_points(self):
        dawarich = FakeDawarich(app_points_on_the_bus(BUS_STOOD_STILL))
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())
        cleanup_log = new_run_log()
        remove_phone_points_during_trips(dawarich, self.settings, cleanup_log)
        self.assertEqual(cleanup_log.phone_points_deleted, len(BUS_STOOD_STILL))
        self.assertEqual(cleanup_log.trips_sent, 0)

        log = new_run_log()
        sync_trips([example_bus_trip_row(type="tram")], True, dawarich, log)
        self.assertEqual(log.trips_sent, 1)
        self.assert_bus_stood_still(dawarich)

    def test_a_steady_ride_converts_as_without_phone_points(self):
        dawarich = FakeDawarich(app_points_on_the_bus(BUS_WENT_STEADILY))
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())

        expected_route, _ = convert_trip(example_bus_trip_row())
        self.assertEqual([point["timestamp"] for point in self.trip_points(dawarich)],
                         [point["timestamp"] for point in expected_route])
        self.assertIsNone(database.get_trip("1234")["sent_anchors"])

    def test_trips_without_phone_fixes_keep_their_fingerprint(self):
        # As before phone fixes existed, so such trips weren't sent again after the update.
        self.assertEqual(trip_fingerprint(example_bus_trip_row()), "a96bdaa690ab6140")

    def test_phone_points_that_reach_dawarich_late_time_the_trip_again(self):
        dawarich = FakeDawarich()
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())
        dawarich.points += app_points_on_the_bus(BUS_STOOD_STILL)

        log = new_run_log()
        remove_phone_points_during_trips(dawarich, self.settings, log)

        self.assertEqual(log.trips_sent, 1)
        self.assert_bus_stood_still(dawarich)
        self.assertTrue(any("reached Dawarich late" in event["message"] for event in database.run_events(log.run_id)))
        self.assertEqual(log.phone_points_deleted, len(BUS_STOOD_STILL))

        later_log = new_run_log()
        sync_trips([example_bus_trip_row()], True, dawarich, later_log)
        self.assertEqual(later_log.trips_sent, 0)

    def test_late_phone_points_that_change_nothing_dont_send_the_trip_again(self):
        dawarich = FakeDawarich()
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())
        point_ids = [point["id"] for point in self.trip_points(dawarich)]
        dawarich.points += app_points_on_the_bus(BUS_WENT_STEADILY)

        remove_phone_points_during_trips(dawarich, self.settings, new_run_log())
        log = new_run_log()
        sync_trips([example_bus_trip_row()], True, dawarich, log)

        self.assertEqual([point["id"] for point in self.trip_points(dawarich)], point_ids)
        self.assertEqual(log.trips_sent, 0)

    def test_phone_points_reaching_dawarich_days_later_dont_change_the_trip(self):
        # It would lose a transport mode corrected in Dawarich meanwhile.
        dawarich = FakeDawarich()
        sync_trips([example_bus_trip_row()], True, dawarich, new_run_log())
        with database.connect() as connection:
            connection.execute("UPDATE trips SET sent_at = '2026-11-10T12:00:00+00:00'")
        point_ids = [point["id"] for point in self.trip_points(dawarich)]
        dawarich.points += app_points_on_the_bus(BUS_STOOD_STILL)

        with unittest.mock.patch("trackfixer.phone_point_cleanup.datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 11, 14, 12, 0, tzinfo=BERLIN)
            fake_datetime.fromisoformat = datetime.fromisoformat
            remove_phone_points_during_trips(dawarich, self.settings, new_run_log())

        self.assertEqual([point["id"] for point in self.trip_points(dawarich)], point_ids)
        self.assertEqual(database.phone_fixes("1234"), [])


if __name__ == "__main__":
    unittest.main()
