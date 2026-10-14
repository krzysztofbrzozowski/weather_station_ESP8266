"""Minimal machine API used by the weather station."""

from datetime import datetime, timezone
import calendar
import json
from pathlib import Path
import time

DEEPSLEEP = 4
PWRON_RESET = 0
WDT_RESET = 3
DEEPSLEEP_RESET = 5
_real_time = time.time
_rtc_offset = 0
_rtc_memory = b''
_state_file = None


def configure_state(directory):
    """Persist simulated RTC memory between launcher processes."""
    global _state_file, _rtc_offset, _rtc_memory
    _state_file = Path(directory) / '.rtc.json'
    try:
        state = json.loads(_state_file.read_text())
        _rtc_offset = state['offset']
        _rtc_memory = state['memory'].encode()
    except (OSError, ValueError, KeyError):
        _rtc_offset = 0
        _rtc_memory = b''


def rtc_time():
    return _real_time() + _rtc_offset


def reset_cause():
    return DEEPSLEEP_RESET if _rtc_memory else PWRON_RESET


def _save_state():
    if _state_file is not None:
        _state_file.write_text(json.dumps({'offset': _rtc_offset, 'memory': _rtc_memory.decode()}))


class WDT:
    def feed(self):
        pass


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2

    def __init__(self, number, mode=IN, pull=None):
        self.number = number
        self.mode = mode
        self.pull = pull
        self._value = 1

    def value(self, value=None):
        if value is not None:
            self._value = int(bool(value))
        return self._value


class I2C:
    def __init__(self, scl, sda, freq=10000):
        self.scl = scl
        self.sda = sda
        self.freq = freq


class ADC:
    def __init__(self, channel):
        self.channel = channel

    def read(self):
        return 900


class RTC:
    ALARM0 = 0
    _alarm_ms = 0

    def datetime(self, value=None):
        global _rtc_offset
        if value is not None:
            _rtc_offset = calendar.timegm((value[0], value[1], value[2], value[4], value[5], value[6])) - _real_time()
            _save_state()
        now = datetime.fromtimestamp(rtc_time(), timezone.utc)
        return (now.year, now.month, now.day, now.weekday(),
                now.hour, now.minute, now.second, 0)

    def memory(self, value=None):
        global _rtc_memory
        if value is not None:
            _rtc_memory = bytes(value)
            _save_state()
        return _rtc_memory

    def irq(self, trigger, wake):
        self.trigger = trigger
        self.wake = wake

    def alarm(self, alarm_id, milliseconds):
        RTC._alarm_ms = milliseconds


class DeepSleep(Exception):
    def __init__(self, milliseconds):
        self.milliseconds = milliseconds
        super().__init__(milliseconds)


def deepsleep(milliseconds=None):
    raise DeepSleep(RTC._alarm_ms if milliseconds is None else milliseconds)
