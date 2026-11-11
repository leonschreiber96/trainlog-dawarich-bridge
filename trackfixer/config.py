"""Settings, read from environment variables. See .env.example for what each one does."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    dawarich_url: str
    dawarich_api_key: str

    trainlog_url: str
    trainlog_username: str
    trainlog_password: str

    data_directory: Path
    nightly_run_time: str  # "HH:MM" in the container's time zone (TZ)
    max_phone_points_deleted_per_run: int
    shortcut_max_silence_minutes: int

    smtp_host: str
    smtp_port: int
    smtp_security: str  # "starttls", "ssl" or "none"
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: str
    app_url: str  # used for links in emails
    port: int = 8000  # of the web UI

    @property
    def trainlog_download_configured(self):
        return bool(self.trainlog_username and self.trainlog_password)

    @property
    def email_configured(self):
        return bool(self.smtp_host and self.email_to)

    @property
    def database_file(self):
        return self.data_directory / "trackfixer.sqlite"

    @property
    def deleted_points_directory(self):
        return self.data_directory / "deleted-points"

    @property
    def latest_trainlog_export_file(self):
        return self.data_directory / "latest-trainlog-export.csv"


def load_settings():
    def text(name, default=""):
        return os.environ.get(name, default).strip()

    def number(name, default):
        return int(text(name) or default)

    missing = [name for name in ("DAWARICH_URL", "DAWARICH_API_KEY") if not text(name)]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)} (see .env.example)")

    return Settings(
        dawarich_url=text("DAWARICH_URL").rstrip("/"),
        dawarich_api_key=text("DAWARICH_API_KEY"),
        trainlog_url=text("TRAINLOG_URL", "https://trainlog.me").rstrip("/"),
        trainlog_username=text("TRAINLOG_USERNAME"),
        trainlog_password=text("TRAINLOG_PASSWORD"),
        data_directory=Path(text("DATA_DIRECTORY", "data")),
        nightly_run_time=text("NIGHTLY_RUN_TIME", "04:00"),
        max_phone_points_deleted_per_run=number("MAX_PHONE_POINTS_DELETED_PER_RUN", 5000),
        shortcut_max_silence_minutes=number("SHORTCUT_MAX_SILENCE_MINUTES", 60),
        smtp_host=text("SMTP_HOST"),
        smtp_port=number("SMTP_PORT", 587),
        smtp_security=text("SMTP_SECURITY", "starttls").lower(),
        smtp_username=text("SMTP_USERNAME"),
        smtp_password=text("SMTP_PASSWORD"),
        email_from=text("EMAIL_FROM") or text("SMTP_USERNAME"),
        email_to=text("EMAIL_TO"),
        app_url=text("APP_URL").rstrip("/"),
        port=number("PORT", 8000),
    )
