import json
import math
import unittest
import unittest.mock
from datetime import datetime

from trackfixer import database, runner
from trackfixer.converter import convert_trip, decode_path, distance_meters
from trackfixer.drawn_trips import MAX_VERTEX_SPACING_METERS, sync_drawn_trips, trip_from_form
from trackfixer.phone_point_cleanup import remove_phone_points_during_trips
from trackfixer.phone_points import distance_to_route_meters, is_trip_point
from trackfixer.run_log import RunLog
from trackfixer.trip_sync import route_of_trip, sync_trips, trip_fingerprint
from trackfixer.web import create_app

from .helpers import (BERLIN, HOME, FakeDawarich, example_bus_trip_row, example_day_points,
                      meters_to_latitude_longitude, temporary_settings)

DRAWN_TRACKER_ID = "trackfixer-drawn-1"


def new_run_log():
    return RunLog(database.start_run("manual"))


def berlin_timestamp(hours, minutes, seconds=0):
    return int(datetime(2026, 11, 10, hours, minutes, seconds, tzinfo=BERLIN).timestamp())


def longitude_latitude(position):
    latitude, longitude = meters_to_latitude_longitude(position)
    return [longitude, latitude]


def walk_form(**changes):
    """The example day's walk from home to the bus stop (12:05-12:15:30), as the editor sends it:
    a start, a leg along the way that follows the road, and a straight leg to the stop."""
    home, corner, near_the_stop, stop = (longitude_latitude(position)
                                         for position in [HOME, (40, -100), (40, -620), (0, -700)])
    form = {
        "type": "walk",
        "origin": "Home",
        "destination": "Elm Street",
        "start_timestamp": str(berlin_timestamp(12, 5)),
        "end_timestamp": str(berlin_timestamp(12, 15, 30)),
        "start_datetime": "2026-11-10 12:05:00",
        "drawing": json.dumps({"legs": [[home], [home, corner, near_the_stop], [stop]]}),
    }
    form.update(changes)
    return form


def save_walk(drawn_trip_id=None, **changes):
    row, drawing = trip_from_form(walk_form(**changes))
    return database.save_drawn_trip(drawn_trip_id, row, drawing)


def drawn_points(dawarich):
    return [point for point in dawarich.points if point.get("tracker_id") == DRAWN_TRACKER_ID]


class DrawnTripFormTest(unittest.TestCase):
    def test_a_drawn_trip_converts_like_a_trainlog_trip(self):
        row, _ = trip_from_form(walk_form())
        route, warnings = convert_trip(dict(row, uid="drawn-1"))

        self.assertEqual(warnings, [])
        self.assertEqual(route[0]["timestamp"], berlin_timestamp(12, 5))
        self.assertEqual(route[-1]["timestamp"], berlin_timestamp(12, 15, 30))
        self.assertEqual([route[0]["longitude"], route[0]["latitude"]], [round(c, 5) for c in longitude_latitude(HOME)])
        self.assertEqual(row["utc_start_datetime"], "2026-11-10 11:05:00")
        self.assertEqual(row["start_datetime"], "2026-11-10 12:05:00")

    def test_follows_the_drawn_route_within_2_meters(self):
        # A quarter circle of 200 m radius, as routing would draw a curved street.
        curve = [longitude_latitude((200 * math.sin(math.radians(angle)), 200 * math.cos(math.radians(angle))))
                 for angle in range(0, 91, 3)]
        # Under 2 minutes, so no points are kept as checkpoints: only the tolerance decides.
        row, _ = trip_from_form(walk_form(drawing=json.dumps({"legs": [[curve[0]], curve[1:]]}),
                                          end_timestamp=str(berlin_timestamp(12, 6, 50))))
        route, _ = route_of_trip(dict(row, uid="drawn-1"))

        for longitude, latitude in curve:
            vertex = {"longitude": longitude, "latitude": latitude}
            self.assertLessEqual(distance_to_route_meters(vertex, route), 2.5)

    def test_long_straight_lines_get_points_in_between(self):
        start, three_km_south = longitude_latitude((0, 0)), longitude_latitude((0, -3000))
        row, _ = trip_from_form(walk_form(drawing=json.dumps({"legs": [[start], [three_km_south]]})))

        path = decode_path(row["path"])
        self.assertGreater(len(path), 10)
        self.assertLessEqual(max(distance_meters(a, b) for a, b in zip(path, path[1:])),
                             MAX_VERTEX_SPACING_METERS + 1)

    def test_says_what_is_missing_or_wrong(self):
        home = longitude_latitude(HOME)
        cases = {
            "Draw the route": walk_form(drawing=json.dumps({"legs": [[home]]})),
            "must end after it starts": walk_form(end_timestamp=str(berlin_timestamp(12, 0))),
            "Set the day": walk_form(start_timestamp=""),
            "what kind of trip": walk_form(type="teleport"),
            "Name where": walk_form(origin="  "),
            "couldn't be read": walk_form(drawing="not json"),
            "outside the map": walk_form(drawing=json.dumps({"legs": [[[200, 10]], [home]]})),
        }
        for expected_message, form in cases.items():
            with self.subTest(expected_message):
                with self.assertRaisesRegex(ValueError, expected_message):
                    trip_from_form(form)


class DrawnTripSyncTest(unittest.TestCase):
    def setUp(self):
        self.settings = temporary_settings()
        self.dawarich = FakeDawarich()

    def test_is_sent_as_its_own_tracker_with_its_mode_of_transport(self):
        save_walk()
        log = new_run_log()
        sync_drawn_trips(self.dawarich, log)

        self.assertEqual(log.trips_sent, 1)
        self.assertGreater(len(drawn_points(self.dawarich)), 2)
        self.assertEqual({str(point["motion_data"]) for point in drawn_points(self.dawarich)},
                         {"{'motion': ['walking'], 'motion_authoritative': True}"})
        self.assertEqual(database.get_trip("drawn-1")["status"], database.IN_DAWARICH)

        sync_drawn_trips(self.dawarich, new_run_log())
        self.assertEqual(database.get_trip("drawn-1")["sent_point_count"], len(drawn_points(self.dawarich)))

    def test_leaving_no_phone_points_out_changes_nothing(self):
        row, _ = trip_from_form(walk_form(ignored_phone_fixes="[]"))
        self.assertNotIn("ignored_phone_fixes", row)
        self.assertEqual(trip_fingerprint(dict(row, uid="drawn-1")),
                         trip_fingerprint(dict(trip_from_form(walk_form())[0], uid="drawn-1")))

    def test_is_timed_by_the_phone_points_of_the_walk(self):
        # The app's first point of the walk, 100 m from home at 12:08:40: evenly spread over the
        # 10.5 minutes, the walk would be there at 12:06:30.
        dawarich = FakeDawarich(example_day_points())
        save_walk()
        sync_drawn_trips(dawarich, new_run_log())

        self.assertIn(berlin_timestamp(12, 8, 40), [point["timestamp"] for point in drawn_points(dawarich)])
        self.assertIn("Timed with", database.get_trip("drawn-1")["status_detail"])

    def test_phone_points_left_out_on_the_map_dont_time_it(self):
        dawarich = FakeDawarich(example_day_points())
        save_walk(ignored_phone_fixes=json.dumps([berlin_timestamp(12, 8, 40)]))
        sync_drawn_trips(dawarich, new_run_log())

        self.assertNotIn(berlin_timestamp(12, 8, 40), [point["timestamp"] for point in drawn_points(dawarich)])

    def test_trips_drawn_before_a_change_of_the_tolerance_are_sent_again(self):
        save_walk()
        with unittest.mock.patch("trackfixer.trip_sync.DRAWN_TRIP_SIMPLIFY_TOLERANCE_METERS", 25):
            sync_drawn_trips(self.dawarich, new_run_log())
        log = new_run_log()
        sync_drawn_trips(self.dawarich, log)

        self.assertEqual(log.trips_sent, 1)
        self.assertEqual(log.trips_resent_after_app_update, 1)

    def test_a_complete_trainlog_export_leaves_drawn_trips_alone(self):
        save_walk()
        sync_drawn_trips(self.dawarich, new_run_log())
        points_before = drawn_points(self.dawarich)

        log = new_run_log()
        sync_trips([example_bus_trip_row()], True, self.dawarich, log)

        self.assertEqual(log.trips_removed, 0)
        self.assertEqual(drawn_points(self.dawarich), points_before)
        self.assertEqual(database.get_trip("drawn-1")["status"], database.IN_DAWARICH)

    def test_editing_replaces_its_points(self):
        drawn_trip_id = save_walk()
        sync_drawn_trips(self.dawarich, new_run_log())
        save_walk(drawn_trip_id, end_timestamp=str(berlin_timestamp(12, 16)))
        log = new_run_log()
        sync_drawn_trips(self.dawarich, log)

        self.assertEqual(drawn_points(self.dawarich)[-1]["timestamp"], berlin_timestamp(12, 16))
        messages = [event["message"] for event in database.run_events(log.run_id)]
        self.assertTrue(any("changed on the Drawn trips page (utc_end_datetime)" in message for message in messages))

    def test_deleting_removes_its_points(self):
        drawn_trip_id = save_walk()
        sync_drawn_trips(self.dawarich, new_run_log())
        database.delete_drawn_trip(drawn_trip_id)
        log = new_run_log()
        sync_drawn_trips(self.dawarich, log)

        self.assertEqual(drawn_points(self.dawarich), [])
        self.assertEqual(log.trips_removed, 1)
        self.assertEqual(database.get_trip("drawn-1")["status"], database.REMOVED)
        self.assertEqual(database.get_trip("drawn-1")["status_detail"], "Deleted on the Drawn trips page.")

    def test_phone_points_during_it_are_deleted(self):
        dawarich = FakeDawarich(example_day_points())
        save_walk()
        sync_drawn_trips(dawarich, new_run_log())
        log = new_run_log()
        remove_phone_points_during_trips(dawarich, self.settings, log)

        start, end = berlin_timestamp(12, 5), berlin_timestamp(12, 15, 30)
        phone_points_left = [point for point in dawarich.points if not is_trip_point(point)
                             and start <= point["timestamp"] <= end]
        self.assertEqual(phone_points_left, [])
        self.assertEqual(log.phone_points_deleted, 7)  # 6 from the app while walking, 1 from the Shortcut
        self.assertEqual(database.get_trip("drawn-1")["phone_points_deleted"], 7)

    def test_a_drawn_trip_run_skips_trainlog_and_health_checks(self):
        save_walk()
        with unittest.mock.patch("trackfixer.runner.DawarichClient", return_value=self.dawarich):
            run_id = runner.run(self.settings, runner.DRAWN_TRIP)

        messages = [event["message"] for event in database.run_events(run_id)]
        self.assertEqual(database.get_run(run_id)["trips_sent"], 1)
        self.assertFalse(any("trainlog" in message for message in messages))
        self.assertFalse(any("Shortcut" in message for message in messages))


class DrawnTripPagesTest(unittest.TestCase):
    def setUp(self):
        self.settings = temporary_settings()
        self.client = create_app(self.settings).test_client()
        start_run = unittest.mock.patch("trackfixer.web.runner.start_run_in_background", return_value=True)
        self.start_run = start_run.start()
        self.addCleanup(start_run.stop)

    def test_saving_a_trip_stores_it_and_starts_a_run(self):
        self.assertEqual(self.client.get("/drawn-trips/new").status_code, 200)
        response = self.client.post("/drawn-trips/new", data=walk_form())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(json.loads(database.get_drawn_trip(1)["csv_row"])["origin_station"], "Home")
        self.start_run.assert_called_once_with(self.settings, runner.DRAWN_TRIP)
        self.assertIn("Home → Elm Street", self.client.get("/drawn-trips").get_data(as_text=True))
        self.assertIn("Elm Street", self.client.get("/drawn-trips/1").get_data(as_text=True))

    def test_a_trip_that_cant_be_saved_is_shown_again_with_the_reason(self):
        response = self.client.post("/drawn-trips/new", data=walk_form(origin="Somewhere", type="teleport"))

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose what kind of trip it was.", page)
        self.assertIn('value="Somewhere"', page)
        self.assertIsNone(database.get_drawn_trip(1))
        self.start_run.assert_not_called()

    def test_deleting_a_trip(self):
        self.client.post("/drawn-trips/new", data=walk_form())
        response = self.client.post("/drawn-trips/1/delete")

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(database.get_drawn_trip(1))
        self.assertEqual(self.client.get("/drawn-trips/1").status_code, 404)

    def test_the_map_gets_dawarichs_points_of_the_day(self):
        dawarich = FakeDawarich(example_day_points())
        with unittest.mock.patch("trackfixer.web.DawarichClient", return_value=dawarich):
            response = self.client.get(f"/drawn-trips/dawarich-points?start={berlin_timestamp(12, 0)}"
                                       f"&end={berlin_timestamp(13, 0)}")
            too_long = self.client.get(f"/drawn-trips/dawarich-points?start=0&end={berlin_timestamp(13, 0)}")

        points = response.get_json()
        self.assertEqual(len(points), len(dawarich.get_points(berlin_timestamp(12, 0), berlin_timestamp(13, 0))))
        self.assertEqual({point["is_trip_point"] for point in points}, {True, False})  # old trainlog import, phone
        self.assertIsInstance(points[0]["latitude"], float)
        self.assertEqual(too_long.status_code, 400)

    def test_the_map_shows_which_phone_points_time_the_trip(self):
        dawarich = FakeDawarich(example_day_points())
        first_point_of_the_walk = berlin_timestamp(12, 8, 40)

        def fixes(**changes):
            with unittest.mock.patch("trackfixer.web.DawarichClient", return_value=dawarich):
                response = self.client.post("/drawn-trips/phone-anchors", data=walk_form(origin="", **changes))
            self.assertEqual(response.status_code, 200)
            return {fix["timestamp"]: fix["status"] for fix in response.get_json()["fixes"]}

        # The app's 6 points of the walk; not the Shortcut's (no accuracy) at 12:15:15.
        self.assertEqual(len(fixes()), 6)
        self.assertEqual(fixes()[first_point_of_the_walk], "used")
        self.assertEqual(fixes(ignored_phone_fixes=json.dumps([first_point_of_the_walk]))[first_point_of_the_walk],
                         "ignored")
        self.assertEqual(fixes(drawing="not json"), {})

    def test_the_map_still_shows_phone_points_deleted_for_the_trip(self):
        dawarich = FakeDawarich(example_day_points())
        self.client.post("/drawn-trips/new", data=walk_form())
        sync_drawn_trips(dawarich, new_run_log())
        remove_phone_points_during_trips(dawarich, self.settings, new_run_log())

        with unittest.mock.patch("trackfixer.web.DawarichClient", return_value=dawarich):
            response = self.client.post("/drawn-trips/phone-anchors", data=dict(walk_form(), drawn_trip_id="1"))
        self.assertIn("used", {fix["status"] for fix in response.get_json()["fixes"]})

    def test_the_trip_list_links_drawn_trips_to_their_editor(self):
        self.client.post("/drawn-trips/new", data=walk_form())
        sync_drawn_trips(FakeDawarich(), new_run_log())

        self.assertIn('href="/drawn-trips/1"', self.client.get("/trips").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
