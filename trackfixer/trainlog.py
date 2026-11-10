"""Getting trips out of trainlog.

trainlog has no official API for this, so the download does what a browser does:
log in with the normal login form, then download the CSV export with the session
cookie. trainlog's login has a mode meant for scripts (?raw=1) that answers
200/401/429 instead of redirecting, so failures are easy to tell apart.
"""

import csv
import http.cookiejar
import io
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "dawarich-track-fixer"


class TrainlogError(Exception):
    pass


def download_trips_csv(trainlog_url, username, password):
    """The same CSV as the export button on trainlog's website: all of the user's trips."""
    cookies = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    browser.addheaders = [("User-Agent", USER_AGENT)]

    login_form = urllib.parse.urlencode({"username": username, "password": password}).encode()
    try:
        browser.open(f"{trainlog_url}/login/?raw=1", data=login_form, timeout=60).read()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise TrainlogError("trainlog rejected the username or password") from error
        if error.code == 429:
            raise TrainlogError("trainlog blocks logins for now after too many attempts") from error
        raise TrainlogError(f"trainlog login failed with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise TrainlogError(f"Could not reach trainlog at {trainlog_url}: {error.reason}") from error

    export_url = f"{trainlog_url}/u/{urllib.parse.quote(username)}/export"
    try:
        with browser.open(export_url, timeout=300) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except urllib.error.HTTPError as error:
        raise TrainlogError(
            f"trainlog export failed with HTTP {error.code}. "
            "TRAINLOG_USERNAME must be the username, not the email address."
        ) from error

    if "csv" not in content_type:
        raise TrainlogError(f"trainlog sent {content_type or 'something'} instead of a CSV; the login probably failed")
    return body.decode("utf-8-sig")


def parse_trips_csv(csv_text):
    """The trips of a trainlog CSV export, as one dict per row."""
    # Encoded route paths of long trips can be longer than the csv module allows by default.
    csv.field_size_limit(2**31 - 1)
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    missing_columns = {"uid", "path", "utc_start_datetime", "utc_end_datetime"} - set(reader.fieldnames or [])
    if missing_columns:
        raise TrainlogError(f"This doesn't look like a trainlog export: missing columns {sorted(missing_columns)}")
    return list(reader)
