"""Trips drawn on a map in the web UI, for journeys that aren't in trainlog: walks, runs, bike
and car rides that the phone recorded badly.

A drawn trip is stored as a row in the format of trainlog's CSV export, so from then on it's
handled like a trainlog trip: its points go to Dawarich as their own tracker, the phone points
recorded during it are deleted, and its track is checked. Deleting a drawn trip removes its
points from Dawarich in the next run.

Like a trainlog trip, it's timed with the accurate phone points recorded during it (see
phone_anchors.py). On the map, single ones can be left out; their times are kept in the row
as "ignored_phone_fixes", a column trainlog doesn't have.
"""

import json
import math
import re
from datetime import datetime, timezone

from . import database
from .converter import decode_path, distance_meters, encode_path
from .trip_sync import remove_deleted_trip, sync_trip, trip_fingerprint

# What a drawn trip can be (keys are trip types as in trainlog, see trip_sync.MOTION_HINTS_BY_TRIP_TYPE),
# and the profile of routing.openstreetmap.de the map uses to follow roads and paths for it.
DRAWN_TRIP_TYPES = {
    "walk": {"label": "Walk", "routing_profile": "routed-foot"},
    "run": {"label": "Run", "routing_profile": "routed-foot"},
    "cycle": {"label": "Bike", "routing_profile": "routed-bike"},
    "car": {"label": "Car", "routing_profile": "routed-car"},
}

# Long straight lines drawn by hand get extra vertices, so the trip's points aren't far apart in
# time (Dawarich ends a track where two points are more than 30 minutes apart).
MAX_VERTEX_SPACING_METERS = 250

MAX_TRIP_HOURS = 24
MAX_NAME_LENGTH = 200
MAX_DRAWN_VERTICES = 50_000
MAX_IGNORED_PHONE_FIXES = 100_000


def sync_drawn_trips(dawarich, log):
    """Send new and changed drawn trips to Dawarich, and remove deleted ones."""
    rows = database.drawn_trip_rows()
    trips_sent_before = log.trips_sent
    for row in rows:
        sync_trip(row, dawarich, log)
    if log.trips_sent > trips_sent_before:
        log.info(f"{log.trips_sent - trips_sent_before} drawn trip(s) were new or changed and sent to Dawarich.")

    drawn_trip_uids = {row["uid"] for row in rows}
    for trip in database.drawn_trips_not_removed():
        if trip["uid"] not in drawn_trip_uids:
            remove_deleted_trip(trip, dawarich, log)


# --- The form ------------------------------------------------------------------------


def trip_from_form(form):
    """(trainlog CSV row, drawing) from the drawn trip form.
    Raises ValueError with a message for the user if something is missing or wrong."""
    trip_type = form.get("type", "")
    if trip_type not in DRAWN_TRIP_TYPES:
        raise ValueError("Choose what kind of trip it was.")

    origin = form.get("origin", "").strip()
    destination = form.get("destination", "").strip()
    if not origin or not destination:
        raise ValueError("Name where the trip started and where it ended.")
    if len(origin) > MAX_NAME_LENGTH or len(destination) > MAX_NAME_LENGTH:
        raise ValueError(f"Keep the names of start and end under {MAX_NAME_LENGTH} characters.")

    start = form_timestamp(form, "start_timestamp")
    end = form_timestamp(form, "end_timestamp")
    if start is None or end is None:
        raise ValueError("Set the day and the start and end time.")
    if end <= start:
        raise ValueError("The trip must end after it starts.")
    if end - start > MAX_TRIP_HOURS * 60 * 60:
        raise ValueError(f"A drawn trip can last at most {MAX_TRIP_HOURS} hours.")

    drawing = parse_drawing(form.get("drawing"))
    vertices = route_vertices(drawing)
    if len(vertices) < 2:
        raise ValueError("Draw the route on the map: click where the trip started, then along the way to "
                         "where it ended.")

    start_datetime = form.get("start_datetime", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?", start_datetime):
        start_datetime = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S")

    row = {
        "type": trip_type,
        "origin_station": origin,
        "destination_station": destination,
        "start_datetime": start_datetime,  # local time, as trainlog's export has it
        "utc_start_datetime": utc_text(start),
        "utc_end_datetime": utc_text(end),
        "path": encode_path(vertices),
    }
    ignored_phone_fixes = parse_ignored_phone_fixes(form.get("ignored_phone_fixes"))
    if ignored_phone_fixes:  # only then, so trips drawn before this existed keep their fingerprint
        row["ignored_phone_fixes"] = ignored_phone_fixes
    return row, drawing


def parse_ignored_phone_fixes(text):
    """The times of the phone points not to time the trip with, chosen on the map (a JSON list)."""
    try:
        timestamps = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(timestamps, list) or len(timestamps) > MAX_IGNORED_PHONE_FIXES:
        return []
    return sorted({timestamp for timestamp in timestamps if type(timestamp) is int and timestamp > 0})  # not bools


def form_values_for_editing(form):
    """What was entered in the form, to show it again when saving failed."""
    try:
        legs = parse_drawing(form.get("drawing"))["legs"]
    except ValueError:
        legs = []
    return {
        "type": form.get("type", ""),
        "origin": form.get("origin", ""),
        "destination": form.get("destination", ""),
        "start_timestamp": form_timestamp(form, "start_timestamp"),
        "end_timestamp": form_timestamp(form, "end_timestamp"),
        "legs": legs,
        "ignored_phone_fixes": parse_ignored_phone_fixes(form.get("ignored_phone_fixes")),
    }


def stored_values_for_editing(drawn_trip):
    row = json.loads(drawn_trip["csv_row"])
    return {
        "type": row["type"],
        "origin": row["origin_station"],
        "destination": row["destination_station"],
        "start_timestamp": utc_text_to_timestamp(row["utc_start_datetime"]),
        "end_timestamp": utc_text_to_timestamp(row["utc_end_datetime"]),
        "legs": json.loads(drawn_trip["drawing"])["legs"],
        "ignored_phone_fixes": row.get("ignored_phone_fixes", []),
    }


def form_timestamp(form, name):
    try:
        timestamp = int(form.get(name, ""))
    except ValueError:
        return None
    return timestamp if timestamp > 0 else None


def parse_drawing(text):
    """The route as drawn: {"legs": [[[longitude, latitude], ...], ...]}. The first leg is just
    where the trip starts; each further leg is the way to the next point clicked on the map."""
    try:
        drawing = json.loads(text or "")
    except json.JSONDecodeError as error:
        raise ValueError("The drawn route couldn't be read.") from error
    legs = drawing.get("legs") if isinstance(drawing, dict) else None
    if not isinstance(legs, list) or not all(isinstance(leg, list) and leg for leg in legs):
        raise ValueError("The drawn route couldn't be read.")
    if sum(len(leg) for leg in legs) > MAX_DRAWN_VERTICES:
        raise ValueError("The drawn route has too many points.")

    def valid_vertex(vertex):
        return (isinstance(vertex, list) and len(vertex) == 2
                and all(isinstance(number, (int, float)) and math.isfinite(number) for number in vertex)
                and -180 <= vertex[0] <= 180 and -90 <= vertex[1] <= 90)

    if not all(valid_vertex(vertex) for leg in legs for vertex in leg):
        raise ValueError("The drawn route has points outside the map.")
    return {"legs": legs}


def route_vertices(drawing):
    """The drawn route as [longitude, latitude] vertices, without repeats, and with extra
    vertices on long straight lines."""
    vertices = []
    for vertex in (vertex for leg in drawing["legs"] for vertex in leg):
        vertex = [round(vertex[0], 6), round(vertex[1], 6)]
        if vertices and vertex == vertices[-1]:
            continue
        if vertices:
            vertices += points_between(vertices[-1], vertex)
        vertices.append(vertex)
    return vertices


def points_between(start, end):
    """Evenly spaced vertices between two vertices, at most MAX_VERTEX_SPACING_METERS apart."""
    sections = math.ceil(distance_meters(start, end) / MAX_VERTEX_SPACING_METERS)
    return [
        [round(start[0] + (end[0] - start[0]) * step / sections, 6),
         round(start[1] + (end[1] - start[1]) * step / sections, 6)]
        for step in range(1, sections)
    ]


def utc_text(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_text_to_timestamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


# --- The list --------------------------------------------------------------------------


def drawn_trips_for_list():
    """Drawn trips, latest first, with their length and what happened to them so far."""
    entries = []
    for drawn_trip in database.all_drawn_trips():
        row = dict(json.loads(drawn_trip["csv_row"]), uid=database.drawn_trip_uid(drawn_trip["id"]))
        vertices = decode_path(row["path"])
        waiting_for_a_run = drawn_trip["status"] is None or (
            drawn_trip["status"] == database.IN_DAWARICH and drawn_trip["sent_fingerprint"] != trip_fingerprint(row)
        )
        entries.append({
            "id": drawn_trip["id"],
            "type": DRAWN_TRIP_TYPES.get(row["type"], {}).get("label", row["type"]),
            "origin": row["origin_station"],
            "destination": row["destination_station"],
            "start_datetime": row["start_datetime"],
            "utc_start_datetime": row["utc_start_datetime"],
            "kilometers": sum(distance_meters(a, b) for a, b in zip(vertices, vertices[1:])) / 1000,
            # Whole minutes, rounded half up like the editor's JavaScript does (Python's round() wouldn't).
            "minutes": math.floor((utc_text_to_timestamp(row["utc_end_datetime"])
                                   - utc_text_to_timestamp(row["utc_start_datetime"])) / 60 + 0.5),
            "status": None if waiting_for_a_run else drawn_trip["status"],
            "status_detail": "" if waiting_for_a_run else drawn_trip["status_detail"],
            "sent_point_count": drawn_trip["sent_point_count"],
            "phone_points_deleted": drawn_trip["phone_points_deleted"],
        })
    return sorted(entries, key=lambda entry: entry["utc_start_datetime"], reverse=True)
