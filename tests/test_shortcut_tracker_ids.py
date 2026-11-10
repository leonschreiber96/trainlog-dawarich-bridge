import contextlib
import io
import unittest
from datetime import datetime

from trackfixer.health_checks import check_phone_tracker_ids, looks_like_shortcut_point
from trackfixer.shortcut_tracker_ids import fix_shortcut_tracker_ids

from .helpers import APP_TRACKER_ID, FakeDawarich, example_day_points, temporary_settings

NOVEMBER = datetime(2026, 11, 1)


def run_quietly(*arguments):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        fix_shortcut_tracker_ids(*arguments)
    return output.getvalue()


def shortcut_points(dawarich):
    return [point for point in dawarich.points if looks_like_shortcut_point(point)]


class FixShortcutTrackerIdsTest(unittest.TestCase):
    def setUp(self):
        self.settings = temporary_settings()
        self.dawarich = FakeDawarich(example_day_points())

    def test_dry_run_changes_nothing(self):
        output = run_quietly(self.dawarich, self.settings, APP_TRACKER_ID, NOVEMBER, False)
        self.assertIn("2026-11: 64 Shortcut points without tracker id", output)
        self.assertEqual(self.dawarich.points, FakeDawarich(example_day_points()).points)

    def test_shortcut_points_get_the_app_tracker_id_one_second_later(self):
        original_times = sorted(point["timestamp"] for point in shortcut_points(self.dawarich))
        run_quietly(self.dawarich, self.settings, APP_TRACKER_ID, NOVEMBER, True)

        moved = shortcut_points(self.dawarich)
        self.assertEqual({point["tracker_id"] for point in moved}, {APP_TRACKER_ID})
        self.assertEqual(sorted(point["timestamp"] for point in moved), [time + 1 for time in original_times])
        self.assertEqual(len(self.dawarich.points), len(example_day_points()))  # nothing lost, nothing doubled
        self.assertEqual(check_phone_tracker_ids(self.dawarich.points), [])
        self.assertEqual(len(list(self.settings.deleted_points_directory.glob("shortcut-*.json"))), 1)

    def test_running_again_after_a_failed_delete_finishes_the_job(self):
        delete_points = self.dawarich.delete_points
        self.dawarich.delete_points = lambda point_ids: (_ for _ in ()).throw(RuntimeError("Dawarich failed"))
        with self.assertRaises(RuntimeError):
            run_quietly(self.dawarich, self.settings, APP_TRACKER_ID, NOVEMBER, True)

        self.dawarich.delete_points = delete_points
        run_quietly(self.dawarich, self.settings, APP_TRACKER_ID, NOVEMBER, True)

        self.assertEqual({point["tracker_id"] for point in shortcut_points(self.dawarich)}, {APP_TRACKER_ID})
        self.assertEqual(len(self.dawarich.points), len(example_day_points()))


if __name__ == "__main__":
    unittest.main()
