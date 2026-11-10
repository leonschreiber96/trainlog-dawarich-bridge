import unittest

from trackfixer import database
from trackfixer.converter import convert_trip
from trackfixer.phone_point_cleanup import remove_phone_points_during_trips
from trackfixer.run_log import RunLog
from trackfixer.trip_sync import sync_trips

from .helpers import FakeDawarich, example_bus_trip_row, example_day_points, temporary_settings


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

        self.assertEqual(log.phone_points_deleted, 8)
        self.assertEqual(database.get_trip("1234")["phone_points_deleted"], 8)
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


if __name__ == "__main__":
    unittest.main()
