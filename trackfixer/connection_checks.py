"""Testing the connections to Dawarich, trainlog and the mail server, from the Overview page.

Each check does what the app relies on, and returns (whether it worked, a message for the user)."""

import time
from datetime import datetime

from .dawarich import DawarichClient, DawarichError
from .notifications import send_test_email
from .phone_points import is_trip_point
from .trainlog import TrainlogError, check_login

DAY_SECONDS = 24 * 60 * 60


def check_dawarich(settings):
    """Read the last day's points with the API key, as runs do."""
    now = time.time()
    try:
        points = DawarichClient(settings.dawarich_url, settings.dawarich_api_key).get_points(now - DAY_SECONDS, now)
    except DawarichError as error:
        if error.status_code == 401:
            return False, "Dawarich rejected the API key (DAWARICH_API_KEY)."
        return False, str(error)
    except ValueError as error:  # not JSON: probably not Dawarich's API at that address
        return False, f"{settings.dawarich_url} didn't answer like Dawarich's API ({error})."

    phone_points = [point for point in points if not is_trip_point(point)]
    if not phone_points:
        return True, "Connected. Dawarich has no phone points from the last 24 hours."
    latest = datetime.fromtimestamp(max(point["timestamp"] for point in phone_points))
    return True, (f"Connected. {len(phone_points)} phone points in the last 24 hours, "
                  f"the latest at {latest:%Y-%m-%d %H:%M}.")


def check_trainlog(settings):
    """Log in, and check that the export is there (without downloading it)."""
    if not settings.trainlog_download_configured:
        return False, "No trainlog login is set (TRAINLOG_USERNAME and TRAINLOG_PASSWORD)."
    try:
        check_login(settings.trainlog_url, settings.trainlog_username, settings.trainlog_password)
    except TrainlogError as error:
        return False, f"{error}."
    return True, f"Logged in as {settings.trainlog_username}; the trip export is available."


def check_email(settings):
    """Send the test email."""
    if not settings.email_configured:
        return False, "No mail server or recipient is set (SMTP_HOST and EMAIL_TO)."
    try:
        send_test_email(settings)
    except Exception as error:  # smtplib and socket errors, of many kinds
        return False, f"Sending failed: {str(error) or type(error).__name__}"
    return True, f"Sent to {settings.email_to}. If it doesn't arrive, look in the spam folder."
