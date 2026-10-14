"""RTC continuity across deep sleep; never invent a time after power loss."""
import os
import time
import ujson
from ubinascii import hexlify
from machine import RTC
from backup import timestamp, valid_timestamp


class StationClock:
    def __init__(self):
        self.rtc = RTC()
        now = int(time.time())
        try:
            state = ujson.loads(self.rtc.memory())
            if (state.get('version') != 1 or not isinstance(state.get('id'), str)
                    or type(state.get('synced')) is not bool
                    or type(state.get('offset')) is not int
                    or type(state.get('last')) is not int
                    or now < state.get('last', 0)):
                raise ValueError('RTC continuity lost')
            # Flash erasure can leave RTC user memory intact. Require a matching
            # filesystem marker before trusting a session from an earlier boot.
            with open('.clock-session') as marker:
                if marker.read(64) != state['id']:
                    raise ValueError('Filesystem/RTC session mismatch')
        except (OSError, ValueError, TypeError, AttributeError):
            state = {'version': 1, 'id': hexlify(os.urandom(8)).decode(),
                     'synced': False, 'offset': 0}
            try:
                with open('.clock-session.tmp', 'w') as marker:
                    marker.write(state['id'])
                if hasattr(os, 'sync'):
                    os.sync()
                os.rename('.clock-session.tmp', '.clock-session')
                if hasattr(os, 'sync'):
                    os.sync()
            except OSError:
                # Keep sampling/sending even if unrelated files fill the disk.
                # This session will conservatively be treated as new next boot.
                print('Clock continuity marker unavailable')
        self.state = state
        self._save()

    def _save(self):
        self.state['last'] = int(time.time())
        self.rtc.memory(ujson.dumps(self.state).encode())

    def capture(self):
        captured = int(time.time())
        result = {'clock': self.state['id'], 'captured': captured}
        if self.state['synced']:
            result['timestamp'] = timestamp(captured)
        return result

    def synchronize(self, server_time):
        if (not isinstance(server_time, (list, tuple)) or len(server_time) != 8
                or any(type(value) is not int for value in server_time)
                or not 2020 <= server_time[0] <= 2099
                or not 1 <= server_time[1] <= 12 or not 1 <= server_time[2] <= 31
                or not 0 <= server_time[4] <= 23 or not 0 <= server_time[5] <= 59
                or not 0 <= server_time[6] <= 59):
            raise ValueError('Invalid API clock response')
        text = '{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z'.format(
            server_time[0], server_time[1], server_time[2],
            server_time[4], server_time[5], server_time[6])
        if not valid_timestamp(text):
            raise ValueError('Invalid API calendar date')
        before = int(time.time())
        self.rtc.datetime(tuple(server_time))
        if not self.state['synced']:
            self.state['offset'] = int(time.time()) - before
        self.state['synced'] = True
        self._save()

    def stamp(self, record):
        if valid_timestamp(record['data'].get('timestamp')):
            return True
        if (not self.state['synced'] or record.get('clock') != self.state['id']
                or type(record.get('captured')) is not int):
            return False
        record['data']['timestamp'] = timestamp(record['captured'] + self.state['offset'])
        return True
