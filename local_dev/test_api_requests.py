"""HTTP failures must preserve measurements on the station."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from stubs import machine, network

FIRMWARE = Path(__file__).resolve().parents[1] / 'firmware' / 'micropython' / 'src'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(status, data=None):
    return types.SimpleNamespace(status_code=status, text=json.dumps(data or {}), close=Mock())


class APIRequestsTest(unittest.TestCase):
    def setUp(self):
        self.http = types.SimpleNamespace(get=Mock(), post=Mock())
        self.modules = patch.dict(sys.modules, {'urequests': self.http, 'ujson': json})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.module = load_module('station_api_test', FIRMWARE / 'APIrequests.py')
        self.api = self.module.APIRequests('https://server/api/measurements/', 'device-key')

    def test_base_url_is_normalized(self):
        self.assertEqual(self.api.API_url, 'https://server/api/measurements')

    def test_missing_token_never_sends_a_post(self):
        api = self.module.APIRequests('https://server/api/measurements', '')
        with self.assertRaises(OSError):
            api.post({'temperature': 21})
        self.http.post.assert_not_called()

    def test_success_and_idempotent_replay_close_response(self):
        for status in [200, 201]:
            with self.subTest(status=status):
                reply = response(status)
                self.http.post.return_value = reply
                self.assertEqual(self.api.post({'temperature': 21}), status)
                reply.close.assert_called_once()
                self.assertEqual(self.http.post.call_args.kwargs['headers']['Authorization'], 'Token device-key')

    def test_http_errors_raise_and_close_response(self):
        for status in [202, 204, 301, 400, 401, 409, 429, 500, 503]:
            with self.subTest(status=status):
                reply = response(status)
                self.http.post.return_value = reply
                with self.assertRaises(OSError):
                    self.api.post({'temperature': 21})
                reply.close.assert_called_once()

    def test_time_sync_matches_rtc_tuple(self):
        rtc = [2026, 10, 13, 1, 10, 0, 0, 0]
        reply = response(200, {'datetime_now': rtc})
        self.http.get.return_value = reply
        self.assertEqual(self.api.get_server_time(), rtc)
        reply.close.assert_called_once()
        self.assertEqual(self.http.get.call_args.kwargs['url'], 'https://server/api/measurements/timesync')

    def test_get_error_and_malformed_json_close_response(self):
        reply = response(503)
        self.http.get.return_value = reply
        with self.assertRaises(OSError):
            self.api.get('timedelta/300')
        reply.close.assert_called_once()
        reply = response(200)
        reply.text = '{bad'
        self.http.get.return_value = reply
        with self.assertRaises(ValueError):
            self.api.get('timedelta/300')
        reply.close.assert_called_once()

    def test_backup_deleted_only_after_server_accepts_measurement(self):
        with patch.dict(sys.modules, {'APIrequests': self.module, 'machine': machine, 'network': network}):
            backup_module = load_module('station_backup_test', FIRMWARE / 'backup.py')
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                backup = backup_module.Backup('backup', self.api.API_url, 'device-key', 300, 8)
                backup.wlan = types.SimpleNamespace(isconnected=lambda: True)
                backup.write_backup({'temperature': 21, 'mac_address': 'aa:bb:cc:dd:ee:ff'})
                self.http.get.return_value = response(200, {'timestamp': '2026-10-13T10:00:00+00:00'})
                self.http.post.return_value = response(429)
                backup.send_backup(True)
                self.assertEqual(len(os.listdir('backup')), 1)
                self.http.post.return_value = response(201)
                backup.send_backup(True)
                self.assertEqual(os.listdir('backup'), [])
            finally:
                os.chdir(original_cwd)


if __name__ == '__main__':
    unittest.main()
