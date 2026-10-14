"""Bounded oldest-first disk queue with independent, recoverable records."""
import os
import time
import ujson
from APIrequests import APIRequests, APIError
from machine import RTC
import network

MAX_RECORD_BYTES = 2048


def timestamp(seconds):
    dt = time.gmtime(seconds)
    return '{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z'.format(*dt[:6])


def valid_timestamp(value):
    if (not isinstance(value, str) or len(value) not in (19, 20)
            or value[4] != '-' or value[7] != '-' or value[10] != 'T'
            or value[13] != ':' or value[16] != ':'
            or (len(value) == 20 and value[-1] != 'Z')):
        return False
    try:
        year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
        hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    except ValueError:
        return False
    if not 2020 <= year <= 2099 or not 1 <= month <= 12:
        return False
    days = (31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return 1 <= day <= days[month - 1] and 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


class Backup:
    def __init__(self, backup_folder, API_url, AUTH_TOKEN, deep_sleep_t=None,
                 correction_t=None, max_records=144, min_free_bytes=32768,
                 timeout=3, clock=None, feed=None):
        # Legacy timing arguments are accepted, but never used to infer dates.
        self.backup_folder = backup_folder
        self.api = APIRequests(API_url, AUTH_TOKEN, timeout=timeout)
        self.wlan = network.WLAN(network.STA_IF)
        self.clock = clock
        self.feed = feed
        self.max_records = max_records
        self.min_free_bytes = min_free_bytes
        if max_records < 1 or min_free_bytes < 0:
            raise ValueError('Invalid backup limits')
        try:
            os.stat(backup_folder)
        except OSError:
            os.mkdir(backup_folder)
        self._recover()
        self._make_space(0)

    def _path(self, name):
        return self.backup_folder + '/' + name

    def _is_record(self, name):
        return ((name.startswith('q') and name.endswith('.json')
                 and name[1:-5].isdigit())
                or (name.startswith('D') and name.endswith('.txt')))

    def _files(self):
        return sorted(name for name in os.listdir(self.backup_folder)
                      if self._is_record(name))

    def _sync(self):
        if self.feed:
            self.feed()
        if hasattr(os, 'sync'):
            os.sync()
        if self.feed:
            self.feed()

    def _read(self, name):
        with open(self._path(name)) as file:
            raw = file.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError('Oversized backup')
        record = ujson.loads(raw)
        if not isinstance(record, dict):
            raise ValueError('Invalid backup')
        if name.startswith('D'):
            record = {'version': 1, 'data': record}
        if record.get('version') != 1 or not isinstance(record.get('data'), dict):
            raise ValueError('Invalid backup format')
        if not isinstance(record['data'].get('mac_address'), str):
            raise ValueError('Missing measurement MAC')
        return record

    def _recover(self):
        for name in os.listdir(self.backup_folder):
            if self.feed:
                self.feed()
            if name.endswith('.tmp') and (self._is_record(name[:-4]) or name == '.clock.json.tmp'):
                try:
                    if name == '.clock.json.tmp':
                        self._read_clock(name)
                    else:
                        self._read(name)
                except (ValueError, TypeError):
                    os.remove(self._path(name))
                    print('Discarded incomplete backup write')
                else:
                    os.rename(self._path(name), self._path(name[:-4]))
                    self._sync()
        self._sync()

    def _read_clock(self, name='.clock.json'):
        with open(self._path(name)) as file:
            mapping = ujson.loads(file.read(512))
        if (not isinstance(mapping, dict) or not isinstance(mapping.get('clock'), str)
                or type(mapping.get('offset')) is not int):
            raise ValueError('Invalid clock mapping')
        return mapping

    def _remember_clock(self):
        try:
            mapping = self._read_clock()
        except (OSError, ValueError, TypeError):
            mapping = None
        if self.clock is None or not self.clock.state['synced']:
            return mapping
        current = {'clock': self.clock.state['id'], 'offset': self.clock.state['offset']}
        if current != mapping:
            # Finish converting the previous session before replacing its map.
            # A power failure mid-conversion leaves the old map recoverable.
            if mapping is not None:
                for name in self._files():
                    if self.feed:
                        self.feed()
                    try:
                        record = self._read(name)
                    except (OSError, ValueError, TypeError):
                        continue
                    if self._stamp(record, mapping):
                        self._write(name, record)
            self._write('.clock.json', current)
        return current

    def _stamp(self, record, mapping):
        if (valid_timestamp(record['data'].get('timestamp')) or mapping is None
                or record.get('clock') != mapping['clock']
                or type(record.get('captured')) is not int):
            return False
        record['data']['timestamp'] = timestamp(record['captured'] + mapping['offset'])
        return True

    def _make_space(self, size, exclude=None, adding=False):
        files = self._files()
        while True:
            fs = os.statvfs(self.backup_folder)
            block = fs[1] or fs[0]
            free = block * fs[4]
            reserve = self.min_free_bytes + size + 2 * block
            if len(files) + int(adding) <= self.max_records and free >= reserve:
                return
            candidates = [name for name in files if name != exclude]
            if not candidates:
                raise OSError('Not enough free space for a safe backup write')
            oldest = candidates[0]
            os.remove(self._path(oldest))
            files.remove(oldest)
            self._sync()
            print('Buffer full: discarded oldest measurement')

    def _write(self, name, record, adding=False):
        raw = ujson.dumps(record)
        size = len(raw.encode('utf-8'))
        if size > MAX_RECORD_BYTES:
            raise ValueError('Measurement exceeds backup size limit')
        self._make_space(size, exclude=name, adding=adding)
        with open(self._path(name + '.tmp'), 'w') as file:
            file.write(raw)
        self._sync()
        os.rename(self._path(name + '.tmp'), self._path(name))
        self._sync()

    def write_backup(self, data, capture=None):
        files = self._files()
        sequence = max([int(name[1:-5]) for name in files
                        if name.startswith('q')] or [0]) + 1
        record = {'version': 1, 'data': dict(data)}
        if capture is None and self.clock is not None:
            capture = self.clock.capture()
        if capture is not None:
            record['clock'] = capture['clock']
            record['captured'] = capture['captured']
            if capture.get('timestamp'):
                record['data']['timestamp'] = capture['timestamp']
        elif not valid_timestamp(record['data'].get('timestamp')):
            dt = RTC().datetime()
            if dt[0] >= 2020:
                record['data']['timestamp'] = '{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z'.format(
                    dt[0], dt[1], dt[2], dt[4], dt[5], dt[6])
        name = 'q{:010d}.json'.format(sequence)
        self._write(name, record, adding=True)
        return name

    def get_backup_list(self):
        return bool(self._files())

    def send_backup(self, sync=None, limit=6, feed=None, deadline_ms=None):
        if sync is False:
            return 0
        mapping = self._remember_clock()
        sent = 0
        attempted = 0
        for name in self._files():
            if feed:
                feed()
            if (attempted >= limit or not self.wlan.isconnected()
                    or (deadline_ms is not None
                        and time.ticks_diff(deadline_ms, time.ticks_ms()) <= 0)):
                break
            try:
                record = self._read(name)
            except (ValueError, TypeError):
                os.remove(self._path(name))
                print('Discarded unreadable backup')
                continue
            except OSError:
                continue  # A low-space eviction may have removed this file.
            if record.get('rejected') in (400, 409, 422):
                continue  # Retain for inspection/eviction; do not starve valid data.
            if not valid_timestamp(record['data'].get('timestamp')):
                if not self._stamp(record, mapping):
                    print('Backup time unknown; record retained until buffer eviction')
                    continue
                self._write(name, record)  # Persist the API identity before POST.
            try:
                attempted += 1
                self.api.post(record['data'])
            except APIError as error:
                if error.status_code in (400, 409, 422):
                    record['rejected'] = error.status_code
                    self._write(name, record)
                    print('Rejected backup retained; trying next record:', error)
                    continue
                print('Backup upload stopped:', error)
                break
            except Exception as error:
                print('Backup upload stopped:', error)
                break
            os.remove(self._path(name))
            self._sync()
            sent += 1
            print('Queued measurement sent')
        return sent
