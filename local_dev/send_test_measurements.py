"""Send synthetic readings to the real Observatory API (no ESP8266 required)."""
import argparse
import ast
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

DEFAULT_SETTINGS = Path(__file__).resolve().parents[1] / 'firmware' / 'src' / '.settings'


class TestFailure(Exception):
    """A safe error message that never includes the device token."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Never forward device credentials to a redirected destination.
        return None


def load_config(path):
    try:
        tree = ast.parse(Path(path).read_text())
    except (OSError, SyntaxError, UnicodeError):
        raise TestFailure('Cannot read settings. Copy firmware/src/.settings.example to .settings and configure it.') from None
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise TestFailure('Settings must contain simple literal assignments only.')
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            raise TestFailure('Settings must contain simple literal assignments only.') from None
        name = node.targets[0].id
        if name in ('MEASUREMENT_INTERVAL_S', 'HTTP_TIMEOUT_S', 'BACKUP_MAX_RECORDS',
                    'BACKUP_MIN_FREE_BYTES', 'BACKUP_MAX_SEND') and type(value) is int:
            continue  # Firmware-only numeric controls are not used by this sender.
        if not isinstance(value, str):
            raise TestFailure('Settings values must be strings.')
        values[node.targets[0].id] = value
    url = values.get('API_url', '').rstrip('/')
    token = values.get('AUTH_TOKEN', '')
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TestFailure('API_url must be an HTTP(S) endpoint without credentials, query parameters or fragments.')
    if not token or token == 'Your API token' or any(char.isspace() for char in token):
        raise TestFailure('Set AUTH_TOKEN in the private .settings file.')
    return url, token


def request_json(opener, url, timeout, data=None, token=None):
    headers = {'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
        headers['Authorization'] = 'Token ' + token
    request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                      headers=headers, method='POST' if data is not None else 'GET')
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            payload = json.loads(response.read(65536))
    except HTTPError as error:
        status = error.code
        error.close()
        hint = {401: 'Check the sensor MAC, token and active status.',
                409: 'A different reading already exists at this timestamp.',
                429: 'The server is rate-limiting requests. Try again later.'}.get(status, 'Check the API URL and server logs.')
        raise TestFailure('HTTP {}. {}'.format(status, hint)) from None
    except (URLError, OSError, TimeoutError):
        raise TestFailure('Cannot reach the API. Check the server, network and HTTPS configuration.') from None
    except (ValueError, UnicodeError):
        raise TestFailure('The API returned an invalid JSON response.') from None
    if not isinstance(payload, dict):
        raise TestFailure('The API response must be a JSON object.')
    return status, payload


def server_time(opener, url, timeout):
    _, payload = request_json(opener, url + '/timesync', timeout)
    rtc = payload.get('datetime_now')
    if not isinstance(rtc, list) or len(rtc) != 8 or any(type(value) is not int for value in rtc):
        raise TestFailure('The time-sync response does not contain a valid RTC tuple.')
    try:
        return datetime(rtc[0], rtc[1], rtc[2], rtc[4], rtc[5], rtc[6], tzinfo=timezone.utc)
    except ValueError:
        raise TestFailure('The time-sync response contains an invalid date.') from None


def measurements(mac, count, start, first_temperature, temperature_step, step_seconds):
    for index in range(count):
        yield {
            'mac_address': mac,
            'temperature': round(first_temperature + index * temperature_step, 2),
            'timestamp': (start - timedelta(seconds=(count - 1 - index) * step_seconds)).isoformat(),
        }


def confirm_response(status, response, reading):
    if status not in (200, 201) or type(response.get('id')) is not int or response['id'] <= 0:
        raise TestFailure('The server did not confirm a saved reading.')
    if response.get('created') is not (status == 201):
        raise TestFailure('The server returned an inconsistent creation status.')
    try:
        timestamp = datetime.fromisoformat(response['timestamp'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TestFailure('The server did not confirm the measurement timestamp.') from None
    if timestamp != datetime.fromisoformat(reading['timestamp']):
        raise TestFailure('The saved timestamp does not match the submitted reading.')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mac', required=True, help='MAC registered in Admin > Sensors')
    parser.add_argument('--settings', type=Path, default=DEFAULT_SETTINGS, help='Private configuration file')
    parser.add_argument('--count', type=int, default=10, help='Number of readings (default: 10)')
    parser.add_argument('--temperature', type=float, default=20.0, help='First temperature in degrees Celsius')
    parser.add_argument('--temperature-step', type=float, default=0.5, help='Increase for each reading')
    parser.add_argument('--step-seconds', type=int, default=300, help='Historical spacing (default: 5 minutes)')
    parser.add_argument('--delay', type=float, default=0.25, help='Delay between requests in seconds')
    parser.add_argument('--timeout', type=float, default=10, help='HTTP timeout in seconds')
    parser.add_argument('--dry-run', action='store_true', help='Print payloads without making any HTTP requests')
    args = parser.parse_args(argv)
    args.mac = args.mac.strip().lower().replace('-', ':')
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', args.mac):
        parser.error('--mac must be a MAC address such as aa:bb:cc:dd:ee:ff')
    if not 1 <= args.count <= 100:
        parser.error('--count must be between 1 and 100')
    if args.step_seconds < 1 or (args.count - 1) * args.step_seconds > 365 * 86400:
        parser.error('measurement spacing must be positive and cover at most one year')
    if not math.isfinite(args.delay) or args.delay < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--delay must be finite and non-negative; --timeout must be finite and positive')
    last = args.temperature + (args.count - 1) * args.temperature_step
    if not math.isfinite(args.temperature_step) or not all(math.isfinite(value) and -80 <= value <= 125 for value in (args.temperature, last)):
        parser.error('all generated temperatures must be between -80 and 125 degrees Celsius')
    return args


def main(argv=None):
    args = parse_args(argv)
    confirmed = created = 0
    try:
        url, token = load_config(args.settings)
        opener = build_opener(NoRedirects())
        start = datetime.now(timezone.utc) if args.dry_run else server_time(opener, url, args.timeout)
        print('{} {} synthetic readings for MAC {}.'.format('Previewing' if args.dry_run else 'Sending', args.count, args.mac))
        if args.dry_run:
            print('Dry run: no network requests will be made.')
        else:
            print('Destination: {}'.format(url))
            print('These are test temperatures, not measurements from the BME280.')
        for index, reading in enumerate(measurements(args.mac, args.count, start, args.temperature, args.temperature_step, args.step_seconds)):
            if args.dry_run:
                print(json.dumps(reading))
                continue
            if index:
                time.sleep(args.delay)
            status, response = request_json(opener, url, args.timeout, data=reading, token=token)
            confirm_response(status, response, reading)
            confirmed += 1
            created += int(response['created'])
            print('[{:02d}/{}] {:.2f} C | HTTP {} | id {} | {}'.format(
                index + 1, args.count, reading['temperature'], status, response['id'],
                'created' if response['created'] else 'already saved'))
        if not args.dry_run:
            print('PASS: {}/{} confirmed; {} new, {} already saved. Check Climate > 24 h.'.format(
                confirmed, args.count, created, confirmed - created))
        return 0
    except TestFailure as error:
        print('FAIL: {} Confirmed: {}/{}. No further readings were sent.'.format(error, confirmed, args.count), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
