"""Start the app:

    python -m trackfixer          web UI + nightly runs (what the Docker container does)
    python -m trackfixer run      a single run right now, without the web UI
"""

import argparse
import logging

import waitress

from . import database, runner, scheduler, web
from .config import load_settings

PORT = 8000


def main():
    parser = argparse.ArgumentParser(prog="trackfixer", description="Keeps Dawarich in sync with trainlog.")
    parser.add_argument("command", nargs="?", choices=("serve", "run"), default="serve")
    command = parser.parse_args().command

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    database.init_database(settings.database_file)

    if command == "run":
        runner.run(settings, runner.MANUAL)
        return

    database.mark_interrupted_runs_as_failed()
    scheduler.start_nightly_runs(settings)
    logging.getLogger("trackfixer").info(
        "Web UI on port %s, nightly runs at %s", PORT, settings.nightly_run_time
    )
    waitress.serve(web.create_app(settings), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
