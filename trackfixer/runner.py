"""One run: get trainlog's trips, sync them to Dawarich, delete duplicate phone points,
check tracking health, and email about any problems. Only one run at a time."""

import logging
import threading
import time

from . import database
from .dawarich import DawarichClient
from .health_checks import check_phone_tracker_ids, check_shortcut_heartbeat
from .notifications import send_problem_email
from .phone_point_cleanup import remove_phone_points_during_trips
from .run_log import RunLog
from .trainlog import TrainlogError, download_trips_csv, parse_trips_csv
from .trip_sync import sync_trips

logger = logging.getLogger("trackfixer")

# What started a run:
NIGHTLY = "nightly"
MANUAL = "manual"
UPLOAD = "upload"

_one_run_at_a_time = threading.Lock()


def is_running():
    return _one_run_at_a_time.locked()


def start_run_in_background(settings, trigger, uploaded_csv=None, upload_is_complete_export=False):
    """Returns False if another run is still going."""
    if is_running():
        return False
    threading.Thread(
        target=run, args=(settings, trigger, uploaded_csv, upload_is_complete_export), daemon=True
    ).start()
    return True


def run(settings, trigger, uploaded_csv=None, upload_is_complete_export=False):
    if not _one_run_at_a_time.acquire(blocking=False):
        logger.info("Skipping the %s run: another run is still going", trigger)
        return None
    try:
        run_id = database.start_run(trigger)
        log = RunLog(run_id)
        dawarich = DawarichClient(settings.dawarich_url, settings.dawarich_api_key)
        try:
            csv_text, is_complete_export = get_trainlog_csv(settings, uploaded_csv, upload_is_complete_export, log)
            if csv_text is not None:
                sync_trips_from_csv(csv_text, is_complete_export, dawarich, log)
            remove_phone_points_during_trips(dawarich, settings, log)
            if trigger != UPLOAD:
                run_health_checks(dawarich, settings, log)
        except Exception as error:
            logger.exception("Run %s stopped", run_id)
            log.error(f"The run stopped: {error}")

        finish(settings, log)
        return run_id
    finally:
        _one_run_at_a_time.release()


def get_trainlog_csv(settings, uploaded_csv, upload_is_complete_export, log):
    """(CSV text or None, whether it holds all trips)."""
    if uploaded_csv is not None:
        settings.latest_trainlog_export_file.write_text(uploaded_csv)
        log.info("Using the uploaded trainlog CSV.")
        return uploaded_csv, upload_is_complete_export

    if not settings.trainlog_download_configured:
        log.info("trainlog download is not configured; only uploaded CSVs are used.")
        return None, False

    try:
        csv_text = download_trips_csv(settings.trainlog_url, settings.trainlog_username, settings.trainlog_password)
    except TrainlogError as error:
        log.error(f"Downloading the trainlog export failed: {error}")
        return None, False
    settings.latest_trainlog_export_file.write_text(csv_text)
    log.info("Downloaded the trainlog export.")
    return csv_text, True


def sync_trips_from_csv(csv_text, is_complete_export, dawarich, log):
    try:
        rows = parse_trips_csv(csv_text)
    except TrainlogError as error:
        log.error(str(error))
        return
    sync_trips(rows, is_complete_export, dawarich, log)


def run_health_checks(dawarich, settings, log):
    now = time.time()
    one_day_ago = now - 24 * 60 * 60
    points = dawarich.get_points(one_day_ago, now)
    warnings = check_phone_tracker_ids(points)
    warnings += check_shortcut_heartbeat(points, one_day_ago, now, settings.shortcut_max_silence_minutes)
    for message in warnings:
        log.warning(message)


def finish(settings, log):
    if log.has_errors:
        outcome = database.FAILED_RUN
    elif log.problems:
        outcome = database.WARNINGS
    else:
        outcome = database.OK
    database.finish_run(log.run_id, outcome, log.trips_sent, log.trips_removed, log.phone_points_deleted)

    if log.problems and settings.email_configured:
        try:
            send_problem_email(settings, log.run_id, log.problems)
        except Exception as error:
            logger.exception("Couldn't send the problem email")
            database.add_run_event(log.run_id, "error", f"Couldn't send the problem email: {error}")
