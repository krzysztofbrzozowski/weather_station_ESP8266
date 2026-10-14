"""Validate the real-HTTP test without contacting a live sensor server."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import send_test_measurements as sender


class SendMeasurementsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / '.settings'
        self.config.write_text("API_url = 'http://localhost/api/measurements/'\nAUTH_TOKEN = 'private-test-token'\n")
        self.argv = ['--mac', 'AA-BB-CC-DD-EE-FF', '--settings', str(self.config), '--delay', '0']
        self.now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    def test_exactly_ten_readings_and_no_secret_output(self):
        sent = []
        def accept(opener, url, timeout, data=None, token=None):
            sent.append(data)
            self.assertEqual(token, 'private-test-token')
            return 201, {'id': len(sent), 'created': True, 'timestamp': data['timestamp']}
        output = io.StringIO()
        with patch.object(sender, 'server_time', return_value=self.now), patch.object(sender, 'request_json', side_effect=accept), redirect_stdout(output):
            self.assertEqual(sender.main(self.argv), 0)
        self.assertEqual(len(sent), 10)
        self.assertEqual([row['temperature'] for row in sent], [20 + i * .5 for i in range(10)])
        times = [datetime.fromisoformat(row['timestamp']) for row in sent]
        self.assertEqual((times[-1] - times[0]).total_seconds(), 45 * 60)
        self.assertEqual(times[-1], self.now)
        self.assertEqual(len(set(times)), 10)
        self.assertEqual(sent[0]['mac_address'], 'aa:bb:cc:dd:ee:ff')
        self.assertNotIn('private-test-token', output.getvalue())
        self.assertIn('PASS: 10/10 confirmed', output.getvalue())

    def test_stops_on_first_error_instead_of_claiming_success(self):
        output = io.StringIO()
        with patch.object(sender, 'server_time', return_value=self.now), patch.object(sender, 'request_json', side_effect=sender.TestFailure('HTTP 401.')) as post, redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(sender.main(self.argv), 1)
        self.assertEqual(post.call_count, 1)
        self.assertIn('Confirmed: 0/10', output.getvalue())
        self.assertNotIn('PASS:', output.getvalue())

    def test_dry_run_never_contacts_server(self):
        output = io.StringIO()
        with patch.object(sender, 'server_time') as clock, patch.object(sender, 'request_json') as http, redirect_stdout(output):
            self.assertEqual(sender.main(self.argv + ['--dry-run']), 0)
        clock.assert_not_called()
        http.assert_not_called()
        rows = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(len(rows), 10)
        self.assertNotIn('private-test-token', output.getvalue())

    def test_false_success_and_mismatched_timestamps_rejected(self):
        reading = next(sender.measurements('aa:bb:cc:dd:ee:ff', 1, self.now, 20, .5, 300))
        for status, response in [(202, {}), (201, {'id': 1, 'created': False}),
                                 (201, {'id': True, 'created': True}),
                                 (201, {'id': 1, 'created': True, 'timestamp': '2026-10-13T11:00:00Z'})]:
            with self.subTest(status=status, response=response), self.assertRaises(sender.TestFailure):
                sender.confirm_response(status, response, reading)
        sender.confirm_response(200, {'id': 1, 'created': False, 'timestamp': reading['timestamp']}, reading)

    def test_config_cannot_execute_code_or_embed_credentials_in_url(self):
        self.config.write_text("raise RuntimeError('should never execute')")
        with self.assertRaises(sender.TestFailure):
            sender.load_config(self.config)
        self.config.write_text("API_url='http://user:password@localhost/api/measurements'\nAUTH_TOKEN='private-test-token'")
        with self.assertRaises(sender.TestFailure):
            sender.load_config(self.config)

    def test_firmware_numeric_settings_do_not_break_the_sender(self):
        with self.config.open('a') as file:
            file.write('\nMEASUREMENT_INTERVAL_S=300\nHTTP_TIMEOUT_S=3\nBACKUP_MAX_RECORDS=144\nBACKUP_MIN_FREE_BYTES=32768\nBACKUP_MAX_SEND=6\n')
        self.assertEqual(sender.load_config(self.config), ('http://localhost/api/measurements', 'private-test-token'))

    def test_http_error_does_not_print_response_body_or_token(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('http://localhost', 401, 'private-test-token', {}, io.BytesIO(b'private-test-token'))
        with self.assertRaises(sender.TestFailure) as raised:
            sender.request_json(opener, 'http://localhost', 10, {}, 'private-test-token')
        self.assertIn('HTTP 401', str(raised.exception))
        self.assertNotIn('private-test-token', str(raised.exception))

    def test_credentials_never_follow_a_redirect(self):
        handler = sender.NoRedirects()
        self.assertIsNone(handler.redirect_request(Mock(), Mock(), 307, 'redirect', {}, 'https://other-host/'))


if __name__ == '__main__':
    unittest.main()
