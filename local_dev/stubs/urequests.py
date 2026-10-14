"""Simulated API responses. Requests are printed and never sent."""

from datetime import datetime, timedelta, timezone
import json as json_module


class Response:
    def __init__(self, data, status_code=200):
        self.text = json_module.dumps(data)
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


def get(url, timeout=None):
    print("[local API] GET {}".format(url))
    now = datetime.now(timezone.utc)
    if url.endswith("/timesync"):
        return Response({"datetime_now": [
            now.year, now.month, now.day, now.weekday(),
            now.hour, now.minute, now.second, 0,
        ]})
    if "/timedelta/" in url:
        timestamp = now - timedelta(seconds=int(url.rsplit("/", 1)[1]))
        return Response({"timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S")})
    raise ValueError("No simulated response for {}".format(url))


def post(url, json=None, headers=None, timeout=None):
    print("[local API] POST {}: {}".format(url, json_module.dumps(json)))
    return Response({"ok": True}, status_code=201)
