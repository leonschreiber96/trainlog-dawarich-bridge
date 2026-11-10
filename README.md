# Dawarich track fixer

Keeps Dawarich in sync with trainlog, so every train/bus/metro ride shows up once,
along its real route, with a small web UI and email when something goes wrong.

## The problem it solves

My location history comes from three sources:

| Source | `tracker_id` in Dawarich | Quality |
|---|---|---|
| Dawarich iOS app | the app's device id (see a point's `tracker_id` in Dawarich) | good, but cuts corners on rides |
| iOS Shortcut (every 15 min, OwnTracks endpoint) | same as the app (set via `tid`) | coarse, helps visit detection |
| trainlog.me trips (sent by this app) | `trainlog-<trip uid>`, one per trip | exact route of each ride |

Dawarich builds tracks **separately per `tracker_id`** (`PARTITION BY tracker_id` in its
`app/models/track.rb`). Points from different trackers never end up in the same track. So:

- **A trainlog ride and the phone points of the same ride became two tracks.**
  This app deletes the phone points recorded during each trainlog trip.
- **Shortcut points never joined the app's tracks.** Fixed in the Shortcut: its `tid` field is
  the app's tracker id (Dawarich maps `tid` → `tracker_id`). If the app's id changes (e.g. after
  reinstalling it), the nightly health check emails you; update the Shortcut then.
- **Back-to-back trainlog trips became one track** when they shared one tracker id.
  Each trip now gets its own tracker id, `trainlog-<trip uid>`.

## What a run does

Every night (and on "Run now" or a CSV upload):

1. **Get trainlog's trips.** Logs in to trainlog and downloads the CSV export (the same as
   the export button). Skipped if no trainlog login is configured; then upload CSVs in the UI.
2. **Sync trips** (`trip_sync.py`). New trips are converted (`converter.py`, ported from my
   earlier `csv_to_dawarich.py` script) and sent to Dawarich's points API. Trips whose route or times
   changed get their points deleted and sent again. Trips deleted in trainlog get their points
   removed (only for complete exports, and at most 10 at once). Places (restaurant, POI,
   accommodation), trips without exact times and future trips are not sent.
   No Dawarich imports are used, so nothing is left behind when a trip is replaced.
   Each point carries the trip's mode of transport (`MOTION_HINTS_BY_TRIP_TYPE`): walk, cycle
   and car trips are set for sure; for bus, train, tram, metro, ferry and flights Dawarich only
   accepts a hint and may still detect something else. Wrong modes can be corrected in
   Dawarich's UI; corrections survive track recalculation.
3. **Delete phone points during trips** (`phone_point_cleanup.py`). For trips sent or taken in
   the last 30 days: every phone point inside the trip's time, plus those up to 3 minutes
   before/after that lie within 50 m of the route. Everything deleted is backed up to
   `deleted-points/` first. Nothing is deleted for a trip if most of its phone points are over
   1 km from the route (the trip's time in trainlog is probably wrong), or if a run would
   delete more than `MAX_PHONE_POINTS_DELETED_PER_RUN` points.
4. **Health checks** (`health_checks.py`) on the last 24 hours: app and Shortcut points use the
   same tracker id, and the Shortcut didn't go silent for more than an hour.
5. **Email** if anything above produced a warning or error.

## Setup (Docker Compose)

1. Copy `docker-compose.example.yml` to your config directory as `docker-compose.yml` and
   adjust the two paths (this repository, and the data directory).
2. Copy `.env.example` next to it as `.env` and fill it in.
3. `docker compose up -d --build`, then open `http://<homelab>:8000`.

If the build fails with `additional privileges requested: pass "--allow=fs.read=..."`, add
`COMPOSE_BAKE=false` to the `.env`: newer Docker Compose versions build with "bake", which
won't read a build directory outside the compose directory without that permission.

To update: `git pull` in this repository, then `docker compose up -d --build`.

There's no login: only expose the port on the VPN.

### Moving existing trips over (once)

Trips imported before this app have the tracker id `trainlog-csv` and would duplicate the
trips this app sends. The run warns about them until they're gone.

1. In Dawarich, delete the old trainlog imports (Imports page). This deletes their points too.
2. Set `MAX_PHONE_POINTS_DELETED_PER_RUN` high (e.g. 1000000) and restart the container.
3. Click "Run now". All trips are sent, and phone points during all of them are deleted.
4. Set the limit back and restart.

### Fixing old Shortcut points (once)

Shortcut points from before its `tid` was set have no tracker id, so Dawarich keeps them in
their own tracks (straight lines between two Shortcut points next to the app's track). This
sends each of them again with the app's tracker id (one second later, because Dawarich refuses
two points at the same time and place), then deletes the original. Originals are backed up to
`deleted-points/` first; running it again is harmless.

```sh
docker compose exec dawarich-track-fixer python -m trackfixer fix-shortcut-tracker-ids \
  --tracker-id <the app's tracker id> --since 2025-01-01            # dry run: shows counts per month
# ...same command with --apply to do it, then "Recalculate tracks & stats" in Dawarich
```

## Development

```sh
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements.txt && python -m unittest discover -s tests -t ."
```

The tests use a made-up example day (built in `tests/helpers.py`, modelled on a real problem
day) and a fake Dawarich.
`python -m trackfixer run` does a single run without the web UI.

Settings that rarely change are constants at the top of the modules, e.g. the time buffer and
route distance in `phone_points.py`. After changing `converter.py`, increase
`CONVERTER_VERSION` in `trip_sync.py` so all trips are converted and sent again.
