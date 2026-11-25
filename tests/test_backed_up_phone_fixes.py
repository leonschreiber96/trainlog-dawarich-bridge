import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

from trackfixer import database
from trackfixer.backed_up_phone_fixes import phone_fixes_from_backups

from .helpers import APP_TRACKER_ID, example_bus_trip_row, temporary_settings

# The example bus ride: 11:16-11:25 UTC.
DURING_THE_RIDE = int(datetime(2026, 11, 10, 11, 20, tzinfo=timezone.utc).timestamp())


def backed_up_point(point_id, timestamp, **properties):
    return dict({"id": point_id, "timestamp": timestamp, "latitude": "49.99", "longitude": "10.0",
                 "tracker_id": APP_TRACKER_ID, "accuracy": 2000, "anomaly": True}, **properties)


class PhoneFixesFromBackupsTest(unittest.TestCase):
    def setUp(self):
        self.settings = temporary_settings()
        database.save_trip(example_bus_trip_row(), "fingerprint", database.IN_DAWARICH)
        self.settings.deleted_points_directory.mkdir()
        inaccurate = backed_up_point(1, DURING_THE_RIDE)
        (self.settings.deleted_points_directory / "2026-11-10_23-59-25_run-1.json").write_text(json.dumps([
            inaccurate,
            backed_up_point(2, DURING_THE_RIDE + 3600),  # after the ride
            backed_up_point(3, DURING_THE_RIDE + 60, accuracy=None),  # the Shortcut
            backed_up_point(4, DURING_THE_RIDE + 120, tracker_id="trainlog-99"),  # another trip's point
        ]))
        # The same point in a later backup (e.g. deleted again after being sent late).
        (self.settings.deleted_points_directory / "2026-11-11_04-00-10_run-2.json").write_text(json.dumps([inaccurate]))

    def run_command(self, apply):
        output = io.StringIO()
        with redirect_stdout(output):
            phone_fixes_from_backups(self.settings, apply)
        return output.getvalue()

    def test_a_dry_run_keeps_nothing(self):
        output = self.run_command(apply=False)
        self.assertIn("Dry run: 1 phone points would be kept for 1 trips", output)
        self.assertEqual(database.phone_fixes("1234"), [])

    def test_app_points_during_a_trip_are_kept_once(self):
        self.run_command(apply=True)
        self.assertEqual([fix["timestamp"] for fix in database.phone_fixes("1234")], [DURING_THE_RIDE])
        self.assertIn("Nothing to add.", self.run_command(apply=True))


if __name__ == "__main__":
    unittest.main()
