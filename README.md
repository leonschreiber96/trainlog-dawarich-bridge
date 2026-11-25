# Dawarich track fixer

Keeps Dawarich in sync with trainlog, so every train/bus/metro ride shows up once,
along its real route, with a small web UI and email when something goes wrong.

## The problem it solves

My location history comes from these sources:

| Source | `tracker_id` in Dawarich | Quality |
|---|---|---|
| Dawarich iOS app | the app's device id (see a point's `tracker_id` in Dawarich) | good, but cuts corners on rides |
| iOS Shortcut (every 15 min, OwnTracks endpoint) | same as the app (set via `tid`) | coarse, helps visit detection |
| trainlog.me trips (sent by this app) | `trainlog-<trip uid>`, one per trip | exact route of each ride |
| Drawn trips (drawn on a map in this app) | `trackfixer-drawn-<n>`, one per trip | as exact as drawn |

Dawarich builds tracks **separately per `tracker_id`** (`PARTITION BY tracker_id` in its
`app/models/track.rb`). Points from different trackers never end up in the same track. So:

- **A trainlog ride and the phone points of the same ride became two tracks.**
  This app deletes the phone points recorded during each trainlog trip.
- **Shortcut points never joined the app's tracks.** Fixed in the Shortcut: its `tid` field is
  the app's tracker id (Dawarich maps `tid` → `tracker_id`). If the app's id changes (e.g. after
  reinstalling it), the nightly health check emails you; update the Shortcut then.
- **Back-to-back trainlog trips became one track** when they shared one tracker id.
  Each trip now gets its own tracker id, `trainlog-<trip uid>`.

## Drawn trips

For journeys that aren't in trainlog and that the phone recorded badly (walks, runs, bike and car
rides): on the "Drawn trips" page, draw the route on a map, with the day and start and end time.
The map shows Dawarich's points of that day; click one to use its time as start or end. Phone
points during the trip are shown in orange: those are deleted once it's saved.

With "Follow roads and paths", each click is joined to the previous one along the way found by
routing.openstreetmap.de (FOSSGIS's routing service that openstreetmap.org uses, profile by trip
type). Your browser sends it the two places of each click; nothing else. Without it, clicks are
joined by straight lines. Map tiles come from tile.openstreetmap.org.

The phone points that time the route (see "Timing trips with the phone's points" below) are shown in
green; click one to leave it out, or a dashed one to use it again. Points that were already deleted for
the trip still show there.

A drawn trip is stored as a row in the format of trainlog's CSV export (`drawn_trips.py`), so it
then goes through the same steps as a trainlog trip below: sent as its own tracker with its mode
of transport set for sure, phone points during it deleted, its track checked. Its points follow the
drawn route within 2 m (trainlog trips: 25 m, which would cut corners in a street; both can be set
in the `.env` file). Saving or deleting
one starts a short run that does just that (no trainlog download, no health checks). Deleting a
drawn trip removes its points from Dawarich; the phone points deleted for it stay deleted (they're
in the backups).

## Timing trips with the phone's points

A trip's times are only known where it starts and ends, and at stops with a time in trainlog. trainlog
stores a stop's realtime arrival or departure only where it differs from the schedule, so in a trip with
realtime times, a stop's scheduled time counts as what happened wherever it has no realtime one (the train
was on time there). Only in a trip without any realtime times are its delays spread over the scheduled
stop times. Where a stop's departure is in a later minute than its arrival, the ride waits there from
arrival to departure.
In between, time is spread evenly over the route. That's wrong when the ride wasn't steady: a train that
stood still between two stations, a car in a traffic jam. The phone's points show what really happened,
even though the line it draws between them is bad. So the app's points (not the Shortcut's) recorded during
a trip are kept in the database before they're deleted, and some become **exact anchors**: the second at
which the ride was at a place on the route (`phone_anchors.py`). The converter keeps an anchor's time to the second, and the ride's points follow it.

Which points become anchors, each time the trip is converted:

- Only those within 50 m of the route. Where the route passes a place twice, the pass closer to where
  the ride should be at that time counts.
- Only those with an accuracy of 30 m or better, or else at most 15 minutes before or after trainlog's
  times have the ride there. In trains, phones often report an accuracy of kilometers for points right on
  the tracks; a point really that far off that happens to lie on the route is mostly far from where the
  ride was at that time.
- Not those recorded while waiting at a stop (within 150 m of it, between its arrival and departure):
  trainlog's times already cover that.
- In route order, at plausible speeds: points that would mean going back along the route, or faster
  than a top speed for the kind of trip, are left out (e.g. a stale position).
- Only where they change something: as long as a point is over 60 s off the times the trip has
  without it, the one farthest off becomes an anchor. A ride that went as planned gets none and converts
  exactly as before; a traffic jam typically needs two or three.
- A stop time from a trip with realtime times wins over a phone point that contradicts it (that one is
  left out, and the trip's notes say so). In a trip without realtime times, a phone point wins over a
  scheduled stop time: the delay probably wasn't entered in trainlog.

The points are taken right before a trip is sent, while they're still in Dawarich. Points that reach
Dawarich later (the phone was offline) are added by the next runs, for 48 hours after sending. If they
change the anchors, the trip is sent again, which loses a transport mode corrected in Dawarich
meanwhile; so not later than that. A trip's notes say how many points time it. Planes get no anchors.

## What a run does

Every night (and on "Run now", a CSV upload, or saving a drawn trip):

1. **Get trainlog's trips.** Logs in to trainlog and downloads the CSV export (the same as
   the export button). Skipped if no trainlog login is configured; then upload CSVs in the UI.
2. **Sync trips** (`trip_sync.py`). New trips are converted (`converter.py`, ported from my
   earlier `csv_to_dawarich.py` script) and sent to Dawarich's points API. Trips whose route or times
   changed get their points deleted and sent again. Trips deleted in trainlog get their points
   removed (only for complete exports, and at most 10 at once). Places (restaurant, POI,
   accommodation), trips without exact times and future trips are not sent.
   No Dawarich imports are used, so nothing is left behind when a trip is replaced.
   Before a trip is sent, the accurate phone points recorded during it are kept to time it (see above).
   Then the same for drawn trips: new and edited ones are sent, deleted ones removed.
   Each point carries the trip's mode of transport (`MOTION_HINTS_BY_TRIP_TYPE`): walk, cycle
   and car trips (and runs, which only drawn trips have) are set for sure; for bus, train, tram,
   metro, ferry and flights Dawarich only accepts a hint and may still detect something else.
   Wrong modes can be corrected in Dawarich's UI. Dawarich keeps those corrections when it
   re-classifies a track, but not on "Recalculate tracks & stats": that deletes and rebuilds all
   tracks of a year, corrections included.
3. **Delete phone points during trips** (`phone_point_cleanup.py`). For trips sent or taken in
   the last 30 days: every phone point inside the trip's time, plus those up to 3 minutes
   before/after that lie within 50 m of the route, except those within 150 m of the departure or
   arrival station (waiting or getting off: your walk starts there). Everything deleted is backed up to
   `deleted-points/` first. Nothing is deleted for a trip if most of its phone points are over
   1 km from the route (the trip's time in trainlog is probably wrong), or if a run would
   delete more than `MAX_PHONE_POINTS_DELETED_PER_RUN` points. Accurate phone points that reached
   Dawarich late are kept to time their trip first (see above). (These distances and times are the
   defaults; they can be set in the `.env` file, see `.env.example`.)
   For the same trips, once sent over an hour ago, it checks that Dawarich made a track from the
   trip and cut the phone's track where the trip is. Dawarich only cuts the phone's track there if
   the trip has a track; otherwise, after the phone points are deleted, the phone's track runs
   in a straight line from where the trip starts to where it ends. If either went wrong, the trip's
   points are sent again, once its points are over 6 hours old (then Dawarich builds tracks from them
   in a backfill, like for any late points, without touching other tracks). If that doesn't help
   either, the run warns. "Recalculate tracks & stats" in Dawarich fixes it too, but costs the
   year's manual mode corrections.
4. **Health checks** (`health_checks.py`) on the last 24 hours: app and Shortcut points use the
   same tracker id, and the Shortcut didn't go silent for more than an hour.
5. **Email** if anything above produced a warning or error.

## Setup (Docker Compose)

1. Copy `docker-compose.example.yml` to your config directory as `docker-compose.yml` and
   adjust the two paths (this repository, and the data directory).
2. Copy `.env.example` next to it as `.env` and fill it in.
3. `docker compose up -d --build`, then open `http://<homelab>:8000` (or the `PORT` set in `.env`).
4. On the Overview page, test the connections: "Test" for Dawarich reads the last day's points with the
   API key, "Test" for trainlog logs in and checks the export is there (without downloading it), and
   "Send test email" sends one.

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
# (that deletes the manual transport mode corrections of the years it rebuilds)
```

### Timing old trips with their phone points (once)

Phone points the phone reports as inaccurate only time trips since they're kept for that (see "Timing
trips with the phone's points"). For older trips, they were deleted from Dawarich and are only in
`deleted-points/`. This keeps those recorded during each trip as its phone fixes, so the next run converts
the trip and sends it again (which loses its transport mode corrections in Dawarich). Running it again is
harmless.

```sh
docker compose exec dawarich-track-fixer python -m trackfixer phone-fixes-from-backups   # dry run
docker compose exec dawarich-track-fixer python -m trackfixer phone-fixes-from-backups --apply
```

## Development

```sh
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements.txt && python -m unittest discover -s tests -t ."
```

The tests use a made-up example day (built in `tests/helpers.py`, modelled on a real problem
day) and a fake Dawarich.
`python -m trackfixer run` does a single run without the web UI.

The tuning settings in `.env.example` (distances, time buffers, limits) are read into the constants at
the top of the modules when they're imported. The other constants there describe how Dawarich and
trainlog work and stay in code. After changing `converter.py`, increase
`CONVERTER_VERSION` in `trip_sync.py` so all trips are converted and sent again.
