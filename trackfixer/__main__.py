"""Start the app:

    python -m trackfixer          web UI + nightly runs (what the Docker container does)
    python -m trackfixer run      a single run right now, without the web UI

One-off repair (see shortcut_tracker_ids.py):

    python -m trackfixer fix-shortcut-tracker-ids --tracker-id <app's id> --since 2025-01-01 [--apply]
"""

import argparse
import logging
from datetime import datetime

import waitress

from . import database, runner, scheduler, web
from .config import load_settings
from .dawarich import DawarichClient
from .shortcut_tracker_ids import fix_shortcut_tracker_ids

def main():
    parser = argparse.ArgumentParser(prog="trackfixer", description="Keeps Dawarich in sync with trainlog.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="web UI + nightly runs (the default)")
    commands.add_parser("run", help="a single run right now, without the web UI")
    fix_command = commands.add_parser(
        "fix-shortcut-tracker-ids", help="give old Shortcut points without tracker id the app's tracker id"
    )
    fix_command.add_argument("--tracker-id", required=True, help="the Dawarich app's tracker id")
    fix_command.add_argument("--since", required=True, type=datetime.fromisoformat,
                             help="first day to look at, e.g. 2025-01-01")
    fix_command.add_argument("--apply", action="store_true", help="really change the points (default: dry run)")
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()

    if arguments.command == "fix-shortcut-tracker-ids":
        dawarich = DawarichClient(settings.dawarich_url, settings.dawarich_api_key)
        fix_shortcut_tracker_ids(dawarich, settings, arguments.tracker_id, arguments.since, arguments.apply)
        return

    database.init_database(settings.database_file)
    if arguments.command == "run":
        runner.run(settings, runner.MANUAL)
        return

    database.mark_interrupted_runs_as_failed()
    scheduler.start_nightly_runs(settings)
    logging.getLogger("trackfixer").info(
        "Web UI on port %s, nightly runs at %s", settings.port, settings.nightly_run_time
    )
    waitress.serve(web.create_app(settings), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
