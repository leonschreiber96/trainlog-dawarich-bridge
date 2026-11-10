import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trackfixer import database
from trackfixer.config import Settings

# --- A made-up example day ----------------------------------------------------------
#
# Modelled on a real problem day, but with an invented place, route and device id.
# Times are Berlin time (UTC+1) on 2026-11-10:
#   00:00-12:00  at home; the iOS Shortcut sends a point every 15 minutes
#   12:08-12:15  walk to the bus stop, recorded by the Dawarich app
#   12:16-12:25  bus ride, recorded by the app AND logged in trainlog (an old
#                "trainlog-csv" import with 13 route points, about 1.8 km)
#   12:25-16:15  at the canteen near the destination stop; Shortcut points
#                (the 15:30 one is missing, so the longest silence is 30 minutes)

APP_TRACKER_ID = "00000000-AAAA-BBBB-CCCC-000000000001"
BERLIN = timezone(timedelta(hours=1))
ORIGIN_LATITUDE, ORIGIN_LONGITUDE = 50.0, 10.0  # somewhere made up

HOME = (0, 0)  # (meters east, meters north) of the origin
BUS_ROUTE_CORNERS = [(0, -700), (-500, -700), (-1100, -1100), (-1100, -1700)]
CANTEEN = (-1030, -1720)


def example_day_points():
    """The example day as Dawarich API points, oldest first."""
    points = []

    def add(time_text, position, **properties):
        hours, minutes, seconds = (int(part) for part in time_text.split(":"))
        timestamp = int(datetime(2026, 11, 10, hours, minutes, seconds, tzinfo=BERLIN).timestamp())
        latitude, longitude = meters_to_latitude_longitude(position)
        points.append(dict(properties, timestamp=timestamp, latitude=str(latitude), longitude=str(longitude)))

    def shortcut(time_text, position, wifi):
        add(time_text, position, tracker_id="", battery=80, ssid=wifi, accuracy=None, velocity=None)

    def app(time_text, position):
        add(time_text, position, tracker_id=APP_TRACKER_ID, battery=None, accuracy=15, velocity=1.5)

    for quarter in range(0, 12 * 4 + 1):  # 00:00 to 12:00
        shortcut(f"{quarter // 4:02}:{quarter % 4 * 15:02}:02", HOME, "HomeWifi")
    for quarter in range(13 * 4, 16 * 4 + 2):  # 13:00 to 16:15, except 15:30
        if quarter != 15 * 4 + 2:
            shortcut(f"{quarter // 4:02}:{quarter % 4 * 15:02}:02", CANTEEN, "WorkWifi")
    shortcut("12:15:15", (30, -640), "")  # near the stop, but not on the bus route
    shortcut("12:30:12", CANTEEN, "WorkWifi")

    for time_text, position in [("12:08:40", (40, -100)), ("12:10:14", (40, -250)), ("12:11:21", (40, -380)),
                                ("12:12:44", (40, -480)), ("12:13:45", (40, -560)), ("12:15:02", (40, -620))]:
        app(time_text, position)  # walking to the stop, 90+ m from the bus route
    app("12:16:41", (3, -702))  # waiting at the stop
    trip_start, trip_end = 12 * 3600 + 16 * 60, 12 * 3600 + 25 * 60
    for time_text in ["12:17:41", "12:19:48", "12:20:52", "12:21:56", "12:23:04", "12:24:15"]:
        hours, minutes, seconds = (int(part) for part in time_text.split(":"))
        progress = (hours * 3600 + minutes * 60 + seconds - trip_start) / (trip_end - trip_start)
        x, y = position_along_bus_route(progress)
        app(time_text, (x + 6, y + 6))  # on the bus, a few meters off the route
    app("12:25:33", (-1098, -1700))  # getting off, just after trainlog's arrival time
    app("12:46:18", CANTEEN)

    for index in range(13):
        progress = index / 12
        seconds = round(trip_start + progress * (trip_end - trip_start))
        add(f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}", position_along_bus_route(progress),
            tracker_id="trainlog-csv", battery=None, accuracy=None, velocity=0.0)

    points.sort(key=lambda point: point["timestamp"])
    return [dict(point, id=index + 1) for index, point in enumerate(points)]


def position_along_bus_route(progress):
    """(meters east, meters north) at a share (0 to 1) of the bus route's length."""
    segments = list(zip(BUS_ROUTE_CORNERS, BUS_ROUTE_CORNERS[1:]))
    lengths = [math.dist(start, end) for start, end in segments]
    remaining = progress * sum(lengths)
    for (start, end), length in zip(segments, lengths):
        if remaining <= length:
            share = remaining / length
            return (start[0] + share * (end[0] - start[0]), start[1] + share * (end[1] - start[1]))
        remaining -= length
    return BUS_ROUTE_CORNERS[-1]


def meters_to_latitude_longitude(position):
    east, north = position
    latitude = ORIGIN_LATITUDE + north / 111_320
    longitude = ORIGIN_LONGITUDE + east / (111_320 * math.cos(math.radians(ORIGIN_LATITUDE)))
    return round(latitude, 7), round(longitude, 7)


def encode_path(vertices):
    """Google polyline encoding of [longitude, latitude] vertices, like trainlog's CSV export."""
    def encode_number(value):
        value = ~(value << 1) if value < 0 else value << 1
        chunks = ""
        while value >= 0x20:
            chunks += chr((0x20 | (value & 0x1F)) + 63)
            value >>= 5
        return chunks + chr(value + 63)

    encoded, previous_latitude, previous_longitude = "", 0, 0
    for longitude, latitude in vertices:
        latitude, longitude = round(latitude * 1e5), round(longitude * 1e5)
        encoded += encode_number(latitude - previous_latitude) + encode_number(longitude - previous_longitude)
        previous_latitude, previous_longitude = latitude, longitude
    return encoded


def example_bus_trip_row(uid="1234", **changes):
    """A trainlog CSV row for the bus ride of the example day (12:16-12:25 Berlin time)."""
    bus_route = [[float(point["longitude"]), float(point["latitude"])]
                 for point in example_day_points() if point["tracker_id"] == "trainlog-csv"]
    row = {
        "uid": uid,
        "type": "bus",
        "origin_station": "Elm Street",
        "destination_station": "Harbour Gate",
        "start_datetime": "2026-11-10 12:16:00",
        "utc_start_datetime": "2026-11-10 11:16:00",
        "utc_end_datetime": "2026-11-10 11:25:00",
        "departure_delay": "0",
        "arrival_delay": "0",
        "waypoints": "[]",
        "path": encode_path(bus_route),
    }
    row.update(changes)
    return row


class FakeDawarich:
    """Keeps points in memory and answers like DawarichClient."""

    def __init__(self, points=()):
        self.points = [dict(point) for point in points]
        self.next_id = max((point["id"] for point in self.points), default=0) + 1

    def get_points(self, start_timestamp, end_timestamp):
        return [point for point in self.points if start_timestamp <= point["timestamp"] <= end_timestamp]

    def create_points(self, route_points, tracker_id, motion_hint=None):
        for route_point in route_points:
            self.points.append({
                "id": self.next_id,
                "timestamp": route_point["timestamp"],
                "latitude": str(route_point["latitude"]),
                "longitude": str(route_point["longitude"]),
                "tracker_id": tracker_id,
                "motion_data": dict(motion_hint or {}),
            })
            self.next_id += 1
        self.points.sort(key=lambda point: point["timestamp"])

    def create_raw_points(self, locations):
        """Like Dawarich: a point at the same time and place as an existing one is skipped."""
        existing = {(point["timestamp"], point["latitude"], point["longitude"]) for point in self.points}
        for location in locations:
            longitude, latitude = location["geometry"]["coordinates"]
            properties = location["properties"]
            point = {
                "id": self.next_id,
                "timestamp": properties["timestamp"],
                "latitude": str(latitude),
                "longitude": str(longitude),
                "tracker_id": properties.get("device_id"),
                "battery": int(properties["battery_level"] * 100) if properties.get("battery_level") else None,
                "ssid": properties.get("wifi"),
                "accuracy": properties.get("horizontal_accuracy"),
            }
            if (point["timestamp"], point["latitude"], point["longitude"]) not in existing:
                self.points.append(point)
                self.next_id += 1
        self.points.sort(key=lambda point: point["timestamp"])

    def delete_points(self, point_ids):
        point_ids = set(point_ids)
        self.points = [point for point in self.points if point["id"] not in point_ids]

    def tracker_ids(self):
        return {point.get("tracker_id") for point in self.points}


def temporary_settings(**changes):
    """Settings with a fresh, empty data directory and database."""
    data_directory = Path(tempfile.mkdtemp())
    values = dict(
        dawarich_url="http://dawarich.test", dawarich_api_key="key",
        trainlog_url="http://trainlog.test", trainlog_username="", trainlog_password="",
        data_directory=data_directory, nightly_run_time="04:00",
        max_phone_points_deleted_per_run=5000, shortcut_max_silence_minutes=60,
        smtp_host="", smtp_port=587, smtp_security="starttls", smtp_username="", smtp_password="",
        email_from="", email_to="", app_url="",
    )
    values.update(changes)
    settings = Settings(**values)
    database.init_database(settings.database_file)
    return settings
