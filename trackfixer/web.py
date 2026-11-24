"""The web UI: overview with run history, trip list, drawn trips, and run details.

There's no login: the app is meant to be reachable only through a VPN.
"""

import os
from datetime import datetime

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from . import database, runner
from .converter import trip_time_range
from .dawarich import DawarichClient, DawarichError
from .drawn_trips import (DRAWN_TRIP_TYPES, drawn_trips_for_list, form_values_for_editing,
                          stored_values_for_editing, trip_from_form)
from .notifications import send_test_email
from .phone_anchors import assess_phone_fixes, fixes_from_points
from .phone_points import DRAWN_TRIP_UID_PREFIX, is_drawn_trip, is_trip_point, tracker_id_for_trip
from .scheduler import next_nightly_run

TRIP_STATUS_LABELS = {
    database.IN_DAWARICH: "in Dawarich",
    database.PLANNED: "planned",
    database.SKIPPED: "skipped",
    database.FAILED: "failed",
    database.REMOVED: "deleted",
}

# The drawn trip editor shows Dawarich's points of a day (and of the night after, if a trip runs past midnight).
MAX_MAP_POINTS_SECONDS = 3 * 24 * 60 * 60


def create_app(settings):
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # trainlog exports with long routes get big

    @app.template_filter("local_time")
    def local_time(iso_text):
        if not iso_text:
            return ""
        return datetime.fromisoformat(iso_text).astimezone().strftime("%Y-%m-%d %H:%M")

    @app.template_filter("timestamp_local_time")
    def timestamp_local_time(timestamp):
        if timestamp is None:
            return ""
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")

    @app.context_processor
    def shared_template_values():
        return {"run_is_going": runner.is_running(), "trip_status_labels": TRIP_STATUS_LABELS,
                "drawn_trip_id_of": drawn_trip_id_of}

    @app.get("/")
    def overview():
        return render_template(
            "overview.html",
            settings=settings,
            next_run=next_nightly_run(settings.nightly_run_time),
            runs=database.recent_runs(),
            trip_counts=database.trip_counts_by_status(),
        )

    @app.post("/run")
    def start_run():
        if runner.start_run_in_background(settings, runner.MANUAL):
            flash("Run started.")
        else:
            flash("A run is still going; try again when it's done.")
        return redirect(url_for("overview"))

    @app.post("/upload")
    def upload_csv():
        uploaded_file = request.files.get("trainlog_csv")
        if not uploaded_file or not uploaded_file.filename:
            flash("Choose a trainlog CSV file first.")
            return redirect(url_for("overview"))

        csv_text = uploaded_file.read().decode("utf-8-sig", errors="replace")
        is_complete_export = request.form.get("is_complete_export") == "yes"
        if runner.start_run_in_background(settings, runner.UPLOAD, csv_text, is_complete_export):
            flash(f"Processing {uploaded_file.filename}.")
        else:
            flash("A run is still going; upload the file again when it's done.")
        return redirect(url_for("overview"))

    @app.post("/test-email")
    def test_email():
        try:
            send_test_email(settings)
            flash(f"Test email sent to {settings.email_to}.")
        except Exception as error:
            flash(f"Sending the test email failed: {error}")
        return redirect(url_for("overview"))

    @app.get("/trips")
    def trips():
        status = request.args.get("status") or None
        return render_template(
            "trips.html",
            trips=database.all_trips(status),
            selected_status=status,
            trip_counts=database.trip_counts_by_status(),
        )

    @app.get("/drawn-trips")
    def drawn_trips():
        return render_template("drawn_trips.html", drawn_trips=drawn_trips_for_list(), types=DRAWN_TRIP_TYPES)

    @app.get("/drawn-trips/new")
    def new_drawn_trip():
        return render_drawn_trip_editor(None, {"type": "walk", "legs": []})

    @app.get("/drawn-trips/<int:drawn_trip_id>")
    def edit_drawn_trip(drawn_trip_id):
        drawn_trip = database.get_drawn_trip(drawn_trip_id)
        if drawn_trip is None:
            abort(404)
        return render_drawn_trip_editor(drawn_trip_id, stored_values_for_editing(drawn_trip))

    @app.post("/drawn-trips/new", endpoint="create_drawn_trip")
    @app.post("/drawn-trips/<int:drawn_trip_id>")
    def save_drawn_trip(drawn_trip_id=None):
        if drawn_trip_id is not None and database.get_drawn_trip(drawn_trip_id) is None:
            abort(404)
        try:
            row, drawing = trip_from_form(request.form)
        except ValueError as error:
            return render_drawn_trip_editor(drawn_trip_id, form_values_for_editing(request.form), str(error)), 400
        database.save_drawn_trip(drawn_trip_id, row, drawing)
        start_drawn_trip_run("Saved")
        return redirect(url_for("drawn_trips"))

    @app.post("/drawn-trips/<int:drawn_trip_id>/delete")
    def delete_drawn_trip(drawn_trip_id):
        database.delete_drawn_trip(drawn_trip_id)
        start_drawn_trip_run("Deleted")
        return redirect(url_for("drawn_trips"))

    @app.get("/drawn-trips/dawarich-points")
    def dawarich_points_for_map():
        try:
            start, end = int(request.args["start"]), int(request.args["end"])
        except (KeyError, ValueError):
            abort(400)
        if not 0 < end - start <= MAX_MAP_POINTS_SECONDS:
            abort(400)
        try:
            points = DawarichClient(settings.dawarich_url, settings.dawarich_api_key).get_points(start, end)
        except DawarichError as error:
            return jsonify(error=str(error)), 502
        return jsonify([
            {
                "timestamp": point["timestamp"],
                "latitude": float(point["latitude"]),
                "longitude": float(point["longitude"]),
                "tracker_id": point.get("tracker_id") or "",
                "is_trip_point": is_trip_point(point),
                "accuracy": point.get("accuracy"),
                "anomaly": bool(point.get("anomaly")),
            }
            for point in points
        ])

    @app.post("/drawn-trips/phone-anchors")
    def phone_anchors_for_map():
        """Which phone points would time the drawn trip as it is in the editor (not saved yet):
        those kept for it before (if it was saved) and those in Dawarich now."""
        form = request.form.to_dict()
        form["origin"] = form.get("origin", "").strip() or "-"  # names don't matter here
        form["destination"] = form.get("destination", "").strip() or "-"
        try:
            row, _ = trip_from_form(form)
            start, end = trip_time_range(row)
        except ValueError:
            return jsonify(fixes=[])

        fixes = {}
        drawn_trip_id = request.form.get("drawn_trip_id", type=int)
        if drawn_trip_id is not None:
            fixes = {fix["timestamp"]: fix for fix in database.phone_fixes(database.drawn_trip_uid(drawn_trip_id))}
        try:
            points = DawarichClient(settings.dawarich_url, settings.dawarich_api_key).get_points(start, end)
        except DawarichError as error:
            return jsonify(error=str(error)), 502
        for fix in fixes_from_points(points, start, end):
            fixes.setdefault(fix["timestamp"], fix)
        try:
            _, statuses = assess_phone_fixes(row, list(fixes.values()))
        except ValueError:
            return jsonify(fixes=[])
        return jsonify(fixes=[dict(fix, status=statuses[timestamp]) for timestamp, fix in sorted(fixes.items())
                              if start < timestamp < end])

    def render_drawn_trip_editor(drawn_trip_id, values, error=None):
        uid = database.drawn_trip_uid(drawn_trip_id) if drawn_trip_id is not None else None
        return render_template(
            "drawn_trip.html",
            drawn_trip_id=drawn_trip_id,
            values=values,
            types=DRAWN_TRIP_TYPES,
            own_tracker_id=tracker_id_for_trip(uid) if uid else None,
            error=error,
        )

    def start_drawn_trip_run(what_happened):
        if runner.start_run_in_background(settings, runner.DRAWN_TRIP):
            flash(f"{what_happened}. Dawarich is being updated now; see the run on the Overview page.")
        else:
            flash(f"{what_happened}. A run is still going, so Dawarich is updated in the next run "
                  "(Run now when this one is done, or tonight).")

    @app.get("/runs/<int:run_id>")
    def run_details(run_id):
        run = database.get_run(run_id)
        if run is None:
            abort(404)
        return render_template("run.html", run=run, events=database.run_events(run_id))

    return app


def drawn_trip_id_of(trip_uid):
    """The drawn trip's id for its uid in the trips table, or None for a trainlog trip."""
    return int(trip_uid[len(DRAWN_TRIP_UID_PREFIX):]) if is_drawn_trip(trip_uid) else None
