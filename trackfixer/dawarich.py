"""The parts of Dawarich's REST API this app uses."""

import json
import urllib.error
import urllib.parse
import urllib.request

POINTS_PER_PAGE = 10_000  # Dawarich's maximum
POINTS_PER_CREATE_REQUEST = 1_000
POINTS_PER_DELETE_REQUEST = 5_000  # Dawarich's maximum


class DawarichError(Exception):
    pass


class DawarichClient:
    def __init__(self, url, api_key):
        self.url = url
        self.api_key = api_key

    def get_points(self, start_timestamp, end_timestamp):
        """All points in the time range (both ends included), oldest first.
        Includes points Dawarich flagged as GPS noise ("anomalies")."""
        points = []
        page = 1
        while True:
            query = {
                "start_at": int(start_timestamp),
                "end_at": int(end_timestamp),
                "include_anomalies": "true",
                "order": "asc",
                "per_page": POINTS_PER_PAGE,
                "page": page,
            }
            points_on_page, headers = self._call("GET", "/api/v1/points", query=query)
            points += points_on_page
            if page >= int(headers.get("X-Total-Pages") or 1):
                return points
            page += 1

    def create_points(self, route_points, tracker_id, motion_hint=None):
        """Send route points (dicts with timestamp, latitude, longitude) as one tracker.
        motion_hint: extra properties for every point that tell Dawarich the mode of transport.
        Dawarich skips points that already exist at the same time and place."""
        locations = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point["longitude"], point["latitude"]]},
                "properties": {"timestamp": point["timestamp"], "device_id": tracker_id, **(motion_hint or {})},
            }
            for point in route_points
        ]
        for batch_start in range(0, len(locations), POINTS_PER_CREATE_REQUEST):
            batch = locations[batch_start : batch_start + POINTS_PER_CREATE_REQUEST]
            self._call("POST", "/api/v1/points", body={"locations": batch})

    def delete_points(self, point_ids):
        """Delete points by id. Dawarich then recalculates the tracks they belonged to."""
        for batch_start in range(0, len(point_ids), POINTS_PER_DELETE_REQUEST):
            batch = point_ids[batch_start : batch_start + POINTS_PER_DELETE_REQUEST]
            self._call("DELETE", "/api/v1/points/bulk_destroy", body={"point_ids": batch})

    def _call(self, method, path, query=None, body=None):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)

        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response), response.headers
        except urllib.error.HTTPError as error:
            details = error.read().decode(errors="replace")[:300]
            raise DawarichError(f"Dawarich answered {method} {path} with HTTP {error.code}: {details}") from error
        except urllib.error.URLError as error:
            raise DawarichError(f"Could not reach Dawarich at {self.url}: {error.reason}") from error
