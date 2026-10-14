"""Failure-injection checks for the firmware without hardware or real HTTP."""
import binascii
import calendar
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from stubs import BME280, machine, network
from test_api_requests import FIRMWARE, load_module, response


class SimulatedTime:
    gmtime = staticmethod(time.gmtime)

    def __init__(self, seconds=1760000000):
        self.seconds = seconds
        self.elapsed = 0

    def time(self):
        return self.seconds

    def ticks_ms(self):
        return self.elapsed

    def ticks_add(self, a, b):
        return (a + b) % (1 << 30)

    def ticks_diff(self, a, b):
        return ((a - b + (1 << 29)) % (1 << 30)) - (1 << 29)

    def sleep_ms(self, milliseconds):
        self.elapsed = self.ticks_add(self.elapsed, milliseconds)
        self.seconds += milliseconds / 1000

class SimulatedRTC:
    ALARM0 = 0

    def __init__(self, timer):
        self.timer = timer
        self.memory_value = b''
        self.alarm_ms = None

    def memory(self, value=None):
        if value is not None:
            self.memory_value = value
        return self.memory_value

    def datetime(self, value=None):
        if value is not None:
            self.timer.seconds = calendar.timegm((value[0], value[1], value[2], value[4], value[5], value[6]))
        now = time.gmtime(self.timer.seconds)
        return (now[0], now[1], now[2], now[6], now[3], now[4], now[5], 0)

    def irq(self, **kwargs):
        pass

    def alarm(self, alarm_id, milliseconds):
        self.alarm_ms = milliseconds


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.original_cwd = Path.cwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, self.original_cwd)
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.timer = SimulatedTime()
        self.rtc = SimulatedRTC(self.timer)
        self.feed = Mock()
        self.sleep = Mock(side_effect=machine.DeepSleep(1))
        self.hardware = types.SimpleNamespace(
            RTC=lambda: self.rtc, WDT=lambda: types.SimpleNamespace(feed=self.feed),
            Pin=machine.Pin, I2C=machine.I2C, ADC=machine.ADC,
            DEEPSLEEP=machine.DEEPSLEEP, deepsleep=self.sleep,
            WDT_RESET=machine.WDT_RESET, reset_cause=lambda: machine.PWRON_RESET,
        )
        network.offline = False
        network.WLAN.enabled = network.WLAN.connected = True
        self.http = types.SimpleNamespace(get=Mock(), post=Mock(return_value=response(201)))
        modules = {'machine': self.hardware, 'network': network, 'ujson': json,
                   'urequests': self.http, 'ubinascii': binascii, 'BME280': BME280}
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.api_module = load_module('review_api', FIRMWARE / 'APIrequests.py')
        sys.modules['APIrequests'] = self.api_module
        self.backup_module = load_module('review_backup', FIRMWARE / 'backup.py')
        self.backup_module.time = self.timer
        sys.modules['backup'] = self.backup_module
        self.clock_module = load_module('review_clock', FIRMWARE / 'station_clock.py')
        self.clock_module.time = self.timer
        sys.modules['station_clock'] = self.clock_module
        self.main_module = load_module('review_main', FIRMWARE / 'main.py')
        self.main_module.time = self.timer
        self.config = {'API_url': 'http://example.invalid/api', 'AUTH_TOKEN': 'test',
                       'BACKUP_FOLDER': 'backups', 'SSID': 'wifi', 'PASSWD': 'password',
                       'HTTP_TIMEOUT_S': 3, 'BACKUP_MAX_RECORDS': 3,
                       'BACKUP_MIN_FREE_BYTES': 8192, 'BACKUP_MAX_SEND': 6,
                       'MEASUREMENT_INTERVAL_S': 300}
        self.data = {'temperature': 21, 'humidity': 40, 'pressure': 1013,
                     'mac_address': 'aa:bb:cc:dd:ee:ff'}
        self.http.get.return_value = response(200, {'datetime_now': [2026, 10, 14, 2, 12, 0, 0, 0]})

    def queue(self, **kwargs):
        return self.backup_module.Backup('backups', 'http://example.invalid/api', 'test', **kwargs)

    def run_main(self):
        sys.modules['settings'] = types.SimpleNamespace(load_settings=lambda: self.config)
        with self.assertRaises(machine.DeepSleep):
            self.main_module.main()
        self.sleep.assert_called_once()

    def test_same_second_records_do_not_collide_or_mutate_input(self):
        queue = self.queue()
        first = queue.write_backup(self.data)
        second = queue.write_backup(self.data)
        self.assertNotEqual(first, second)
        self.assertEqual(len(queue._files()), 2)
        self.assertNotIn('timestamp', self.data)
        for name in queue._files():
            self.assertEqual(queue._read(name)['data']['temperature'], 21)

    def test_capacity_survives_restarts_and_evicts_only_oldest(self):
        queue = self.queue(max_records=3)
        names = [queue.write_backup(dict(self.data, temperature=index)) for index in range(3)]
        queue = self.queue(max_records=3)
        newest = queue.write_backup(dict(self.data, temperature=3))
        self.assertEqual(queue._files(), names[1:] + [newest])
        self.assertEqual([queue._read(n)['data']['temperature'] for n in queue._files()], [1, 2, 3])

    def test_low_space_deletes_oldest_and_preserves_unrelated_files(self):
        queue = self.queue(min_free_bytes=8192)
        names = [queue.write_backup(self.data) for _ in range(3)]
        Path('backups/keep.txt').write_text('unrelated')
        def fs(path):
            free = 7 if Path('backups', names[0]).exists() else 20
            return (1024, 1024, 100, free, free, 0, 0, 0, 0, 255)
        with patch.object(self.backup_module.os, 'statvfs', side_effect=fs):
            newest = queue.write_backup(self.data)
        self.assertEqual(queue._files(), names[1:] + [newest])
        self.assertEqual(Path('backups/keep.txt').read_text(), 'unrelated')

    def test_no_space_fails_without_writing_below_reserve(self):
        queue = self.queue()
        with patch.object(self.backup_module.os, 'statvfs', return_value=(4096, 4096, 100, 0, 0, 0, 0, 0, 0, 255)):
            with self.assertRaises(OSError):
                queue.write_backup(self.data)
        self.assertEqual(queue._files(), [])

    def test_complete_temporary_write_recovered_after_rename_failure(self):
        queue = self.queue()
        with patch.object(self.backup_module.os, 'rename', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                queue.write_backup(self.data)
        self.assertEqual(len(list(Path('backups').glob('*.tmp'))), 1)
        recovered = self.queue()
        self.assertEqual(len(recovered._files()), 1)
        self.assertFalse(list(Path('backups').glob('*.tmp')))
        self.assertEqual(recovered._read(recovered._files()[0])['data']['temperature'], 21)

    def test_partial_temporary_write_discarded_without_losing_committed_records(self):
        queue = self.queue()
        name = queue.write_backup(self.data)
        Path('backups', name + '.tmp').write_text('{"version":')
        recovered = self.queue()
        self.assertEqual(recovered._files(), [name])
        self.assertFalse(list(Path('backups').glob('*.tmp')))

    def test_corrupt_committed_file_does_not_block_following_measurement(self):
        queue = self.queue()
        broken = queue.write_backup(self.data)
        queue.write_backup(self.data)
        Path('backups', broken).write_text('{}{}')
        self.assertEqual(queue.send_backup(), 1)
        self.assertFalse(queue.get_backup_list())

    def test_send_order_ignores_directory_listing_order(self):
        queue = self.queue()
        for index in range(3):
            queue.write_backup(dict(self.data, temperature=index))
        listing = os.listdir
        with patch.object(self.backup_module.os, 'listdir', side_effect=lambda p: list(reversed(listing(p)))):
            self.assertEqual(queue.send_backup(), 3)
        self.assertEqual([c.kwargs['json']['temperature'] for c in self.http.post.call_args_list], [0, 1, 2])

    def test_lost_response_retry_uses_persisted_identical_timestamp(self):
        clock = self.clock_module.StationClock()
        queue = self.queue(clock=clock)
        name = queue.write_backup(self.data)
        clock.synchronize([2026, 10, 14, 2, 12, 0, 0, 0])
        self.http.post.side_effect = OSError('response lost after server saved data')
        self.assertEqual(queue.send_backup(), 0)
        original = queue._read(name)['data']['timestamp']
        self.timer.seconds += 300
        self.http.post.side_effect = None
        queue = self.queue(clock=self.clock_module.StationClock())
        self.assertEqual(queue.send_backup(), 1)
        self.assertEqual([c.kwargs['json']['timestamp'] for c in self.http.post.call_args_list], [original, original])

    def test_rate_limit_stops_after_first_request_and_retains_every_record(self):
        queue = self.queue()
        for _ in range(3):
            queue.write_backup(self.data)
        self.http.post.return_value = response(429)
        self.assertEqual(queue.send_backup(), 0)
        self.assertEqual(self.http.post.call_count, 1)
        self.assertEqual(len(queue._files()), 3)

    def test_bad_record_rejection_does_not_block_valid_following_record(self):
        queue = self.queue()
        first = queue.write_backup(self.data)
        queue.write_backup(self.data)
        self.http.post.side_effect = [response(400), response(201)]
        self.assertEqual(queue.send_backup(), 1)
        self.assertEqual(queue._files(), [first])

    def test_http_attempt_limit_also_applies_to_rejected_records(self):
        queue = self.queue()
        for _ in range(3):
            queue.write_backup(self.data)
        self.http.post.return_value = response(409)
        queue.send_backup(limit=2)
        self.assertEqual(self.http.post.call_count, 2)
        self.assertEqual(len(queue._files()), 3)

    def test_rejected_records_do_not_consume_the_next_cycle_upload_budget(self):
        queue = self.queue()
        for _ in range(3):
            queue.write_backup(self.data)
        self.http.post.return_value = response(400)
        queue.send_backup(limit=2)
        self.http.post.reset_mock()
        self.http.post.return_value = response(201)
        self.assertEqual(queue.send_backup(limit=1), 1)
        self.http.post.assert_called_once()
        self.assertEqual(len(queue._files()), 2)

    def test_saved_clock_mapping_survives_power_loss_mid_upload(self):
        clock = self.clock_module.StationClock()
        queue = self.queue(clock=clock)
        first = queue.write_backup(self.data)
        self.timer.seconds += 300
        second = queue.write_backup(self.data)
        clock.synchronize([2026, 10, 14, 2, 12, 0, 0, 0])
        self.http.post.side_effect = OSError('power lost after request')
        queue.send_backup()
        self.assertNotIn('timestamp', queue._read(second)['data'])
        original = queue._read(first)['data']['timestamp']
        self.rtc.memory_value = b''
        rebooted = self.clock_module.StationClock()
        queue = self.queue(clock=rebooted)
        self.http.post.side_effect = None
        self.http.post.reset_mock()
        self.assertEqual(queue.send_backup(), 2)
        times = [call.kwargs['json']['timestamp'] for call in self.http.post.call_args_list]
        self.assertEqual(times[0], original)
        seconds = [calendar.timegm(time.strptime(value, '%Y-%m-%dT%H:%M:%SZ')) for value in times]
        self.assertEqual(seconds[1] - seconds[0], 300)

    def test_old_clock_map_is_not_replaced_if_conversion_is_interrupted(self):
        clock = self.clock_module.StationClock()
        queue = self.queue(clock=clock)
        queue.write_backup(self.data)
        self.timer.seconds += 300
        second = queue.write_backup(self.data)
        clock.synchronize([2026, 10, 14, 2, 12, 0, 0, 0])
        self.http.post.side_effect = OSError('response lost')
        queue.send_backup()
        old_mapping = queue._read_clock()
        self.rtc.memory_value = b''
        new_clock = self.clock_module.StationClock()
        new_clock.synchronize([2026, 10, 14, 2, 13, 0, 0, 0])
        queue.clock = new_clock
        writer = queue._write
        def interrupted(name, record, **kwargs):
            if name == second:
                raise OSError('disk write interrupted')
            return writer(name, record, **kwargs)
        with patch.object(queue, '_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                queue.send_backup()
        self.assertEqual(queue._read_clock(), old_mapping)
        self.http.post.side_effect = None
        self.assertEqual(queue.send_backup(), 2)

    def test_identical_replay_200_removes_record_after_lost_response(self):
        queue = self.queue()
        name = queue.write_backup(self.data)
        self.http.post.side_effect = OSError('ack lost')
        queue.send_backup()
        self.assertEqual(queue._files(), [name])
        self.http.post.side_effect = None
        self.http.post.return_value = response(200)
        self.assertEqual(queue.send_backup(), 1)
        self.assertFalse(queue.get_backup_list())

    def test_deadline_works_across_tick_wrap(self):
        queue = self.queue()
        queue.write_backup(self.data)
        self.timer.elapsed = (1 << 30) - 10
        deadline = self.timer.ticks_add(self.timer.elapsed, 5)
        self.timer.sleep_ms(6)
        self.assertEqual(queue.send_backup(deadline_ms=deadline), 0)
        self.http.post.assert_not_called()

    def test_old_valid_backup_keeps_original_date(self):
        queue = self.queue()
        old = dict(self.data, timestamp='2026-10-13T11:00:00')
        Path('backups/D2026-10-13T11_00_00.txt').write_text(json.dumps(old))
        self.assertEqual(queue.send_backup(), 1)
        self.assertEqual(self.http.post.call_args.kwargs['json']['timestamp'], old['timestamp'])

    def test_unknown_time_from_lost_rtc_session_is_retained_without_inventing_date(self):
        clock = self.clock_module.StationClock()
        queue = self.queue(clock=clock)
        name = queue.write_backup(self.data)
        self.rtc.memory_value = b''
        new_clock = self.clock_module.StationClock()
        new_clock.synchronize([2026, 10, 14, 2, 12, 0, 0, 0])
        queue.clock = new_clock
        queue.send_backup()
        self.http.post.assert_not_called()
        self.assertEqual(queue._files(), [name])

    def test_offline_time_reconstructed_after_deep_sleep_and_keeps_spacing(self):
        clock = self.clock_module.StationClock()
        queue = self.queue(clock=clock)
        queue.write_backup(self.data)
        self.timer.seconds += 300
        clock = self.clock_module.StationClock()
        queue.clock = clock
        queue.write_backup(self.data)
        clock.synchronize([2026, 10, 14, 2, 12, 0, 0, 0])
        queue.send_backup()
        times = [calendar.timegm(time.strptime(c.kwargs['json']['timestamp'], '%Y-%m-%dT%H:%M:%SZ')) for c in self.http.post.call_args_list]
        self.assertEqual(times[1] - times[0], 300)

    def test_wifi_connection_starts_immediately_and_waits_full_timeout(self):
        wlan = types.SimpleNamespace(active=Mock(), connect=Mock(), isconnected=lambda: False)
        self.assertFalse(self.main_module.do_connect(wlan, 'wifi', 'password', self.feed))
        wlan.connect.assert_called_once_with('wifi', 'password')
        self.assertEqual(self.timer.elapsed, 8000)
        self.assertEqual(self.feed.call_count, 40)

    def test_wifi_without_password_is_supported(self):
        wlan = types.SimpleNamespace(active=Mock(), connect=Mock(), isconnected=Mock(side_effect=[False, True]))
        self.assertTrue(self.main_module.do_connect(wlan, 'open-wifi', '', self.feed))
        wlan.connect.assert_called_once_with('open-wifi', '')

    def test_sensor_failure_still_reaches_sleep(self):
        with patch.object(BME280, 'BME280', side_effect=OSError('sensor disconnected')):
            self.run_main()
        self.assertEqual(self.rtc.alarm_ms, 300000)

    def test_config_failure_still_reaches_sleep(self):
        sys.modules['settings'] = types.SimpleNamespace(load_settings=Mock(side_effect=SyntaxError('bad config')))
        with self.assertRaises(machine.DeepSleep):
            self.main_module.main()
        self.sleep.assert_called_once()
        self.assertEqual(self.rtc.alarm_ms, 300000)

    def test_watchdog_reset_sleeps_before_trying_network_again(self):
        self.hardware.reset_cause = lambda: machine.WDT_RESET
        with patch.object(self.main_module, 'run_cycle') as cycle:
            self.run_main()
        cycle.assert_not_called()
        self.assertEqual(self.rtc.alarm_ms, 300000)

    def test_cycle_compensates_awake_time_and_preserves_offline_record(self):
        network.offline = True
        self.run_main()
        self.assertEqual(self.rtc.alarm_ms, 291600)
        self.assertEqual(len(list(Path('backups').glob('q*.json'))), 1)

    def test_storage_failure_still_sends_current_measurement_and_sleeps(self):
        with patch.object(self.backup_module, 'Backup', side_effect=OSError('disk full')):
            self.run_main()
        self.http.post.assert_called_once()
        self.assertEqual(self.http.post.call_args.kwargs['json']['temperature'], 22.5)

    def test_storage_and_network_failure_still_sleep(self):
        network.offline = True
        with patch.object(self.backup_module, 'Backup', side_effect=OSError('disk full')):
            self.run_main()
        self.http.post.assert_not_called()

    def test_time_sync_failure_does_not_delete_unstamped_measurement(self):
        self.http.get.side_effect = OSError('timeout')
        self.run_main()
        self.http.post.assert_not_called()
        self.assertEqual(len(list(Path('backups').glob('q*.json'))), 1)

    def test_impossible_calendar_date_does_not_change_rtc(self):
        clock = self.clock_module.StationClock()
        before = self.timer.seconds
        with self.assertRaises(ValueError):
            clock.synchronize([2026, 2, 30, 0, 12, 0, 0, 0])
        self.assertEqual(self.timer.seconds, before)
        self.assertFalse(clock.state['synced'])

    def test_timestamp_validation_accepts_leap_day_and_rejects_invalid_dates(self):
        valid = self.backup_module.valid_timestamp
        self.assertTrue(valid('2024-02-29T23:59:59Z'))
        self.assertFalse(valid('2026-02-29T23:59:59Z'))
        self.assertFalse(valid('2026-10-14T24:00:00Z'))
        self.assertFalse(valid('2026-aa-bbT12:00:00Z'))

    def test_corrupt_rtc_state_starts_new_unsynchronized_session(self):
        self.rtc.memory_value = json.dumps({'version': 1, 'id': 'old', 'synced': True,
                                           'offset': 'broken', 'last': 0}).encode()
        clock = self.clock_module.StationClock()
        self.assertFalse(clock.state['synced'])
        self.assertNotEqual(clock.state['id'], 'old')

    def test_known_timestamps_can_upload_when_time_sync_is_temporarily_down(self):
        clock = self.clock_module.StationClock()
        clock.synchronize([2026, 10, 14, 2, 12, 0, 0, 0])
        self.http.get.side_effect = OSError('time endpoint down')
        self.run_main()
        self.http.post.assert_called_once()
        self.assertFalse(list(Path('backups').glob('q*.json')))

    def test_backup_write_error_reaches_sleep_and_can_deliver_current_sample(self):
        with patch.object(self.backup_module.Backup, '_write', side_effect=OSError('flash write failed')):
            self.run_main()
        self.http.post.assert_called_once()

    def test_http_timeout_is_passed_to_get_and_post(self):
        api = self.api_module.APIRequests('http://example.invalid/api', 'test', timeout=2)
        api.get_server_time()
        api.post(self.data)
        self.assertEqual(self.http.get.call_args.kwargs['timeout'], 2)
        self.assertEqual(self.http.post.call_args.kwargs['timeout'], 2)

    def test_nonfinite_sensor_value_is_not_sent_or_saved(self):
        sensor = types.SimpleNamespace(values={'temperature': float('nan'), 'humidity': 40, 'pressure': 1013})
        with self.assertRaises(ValueError):
            self.main_module.get_measurement(sensor, self.feed)

    def test_real_bme_driver_busy_timeout_is_bounded(self):
        with patch.dict(sys.modules, {'ustruct': struct}):
            spec = importlib.util.spec_from_file_location('real_bme', FIRMWARE / 'BME280.py')
            module = importlib.util.module_from_spec(spec)
            module.const = lambda value: value
            spec.loader.exec_module(module)
        module.time = self.timer
        sensor = module.BME280.__new__(module.BME280)
        sensor._mode = 4
        sensor.address = 0x76
        sensor._l1_barray = bytearray(1)
        sensor.i2c = types.SimpleNamespace(writeto_mem=Mock(), readfrom_mem=lambda *args: b'\x08')
        with self.assertRaisesRegex(OSError, 'conversion timeout'):
            sensor.read_raw_data([0, 0, 0])
        self.assertEqual(self.timer.elapsed, 1000)


if __name__ == '__main__':
    unittest.main()
