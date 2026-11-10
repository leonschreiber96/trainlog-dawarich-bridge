"""The web UI: overview with run history, trip list, and run details.

There's no login: the app is meant to be reachable only through a VPN.
"""

import os
from datetime import datetime

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from . import database, runner
from .notifications import send_test_email
from .scheduler import next_nightly_run

TRIP_STATUS_LABELS = {
    database.IN_DAWARICH: "in Dawarich",
    database.PLANNED: "planned",
    database.SKIPPED: "skipped",
    database.FAILED: "failed",
    database.REMOVED: "deleted in trainlog",
}


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
        return {"run_is_going": runner.is_running(), "trip_status_labels": TRIP_STATUS_LABELS}

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

    @app.get("/runs/<int:run_id>")
    def run_details(run_id):
        run = database.get_run(run_id)
        if run is None:
            abort(404)
        return render_template("run.html", run=run, events=database.run_events(run_id))

    return app
