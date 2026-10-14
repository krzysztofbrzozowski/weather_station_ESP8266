# Author: Krzysztof Brzozowski

try:
    import urequests
except ImportError:
    import requests as urequests
import ujson


class APIError(OSError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('API HTTP {}'.format(status_code))


class APIRequests:
    def __init__(self, API_url, auth_token, timeout=3):
        self.datetime = None
        self.auth_token = auth_token
        self.headers = {
            'Authorization': 'Token {}'.format(self.auth_token),
            'Content-Type': 'application/json',
        }
        self.API_url = API_url.rstrip('/')
        self.request_code = None
        self.timeout = timeout

    def _get_json(self, url):
        response = urequests.get(url=url, timeout=self.timeout)
        try:
            if not 200 <= response.status_code < 300:
                raise APIError(response.status_code)
            return ujson.loads(response.text)
        finally:
            response.close()

    def get_server_time(self):
        return self._get_json('{}/timesync'.format(self.API_url))['datetime_now']

    def get(self, url):
        return self._get_json('{}/{}'.format(self.API_url, url))

    def post(self, data):
        response = urequests.post(self.API_url, json=data, headers=self.headers, timeout=self.timeout)
        try:
            self.request_code = response.status_code
            if self.request_code not in (200, 201):
                # main.py saves the measurement and Backup.send_backup keeps
                # its file when this raises; rejected data must never be lost.
                raise APIError(self.request_code)
            return self.request_code
        finally:
            response.close()
