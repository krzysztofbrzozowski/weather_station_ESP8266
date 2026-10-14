"""Exercise discovery, remembered profiles, failover and bounded offline waits."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stubs import machine, network
from test_api_requests import FIRMWARE, load_module
from test_reliability import SimulatedTime


class WifiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, original)
        modules = patch.dict('sys.modules', {'machine': machine, 'ujson': json})
        modules.start()
        self.addCleanup(modules.stop)
        import sys
        sys.modules['station_wifi'] = load_module('wifi_connector', FIRMWARE / 'station_wifi.py')
        self.main = load_module('wifi_main', FIRMWARE / 'main.py')
        self.settings = load_module('wifi_settings', FIRMWARE / 'settings.py')
        self.timer = SimulatedTime()
        self.main.time = self.timer
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        patches = [patch.object(network, 'offline', False),
                   patch.object(network.WLAN, 'connected', False),
                   patch.object(network.WLAN, 'enabled', False),
                   patch.object(network.WLAN, 'available_networks', {'office': 'unit-b'})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.wlan = network.WLAN()
        self.profiles = [{'SSID': 'home', 'PASSWD': 'unit-a'}, {'SSID': 'office', 'PASSWD': 'unit-b'},
                         {'SSID': 'open', 'PASSWD': ''}, {'SSID': 'travel', 'PASSWD': 'unit-d'}]

    def connect(self, profiles=None, timeout=8000):
        return self.main.do_connect_profiles(self.wlan, profiles or self.profiles, lambda: None, timeout)

    def cache(self):
        return json.loads(Path('.wifi.json').read_text())

    def test_discovers_available_profile_and_remembers_it_across_restart(self):
        with patch.object(self.wlan, 'connect', wraps=self.wlan.connect) as attempts:
            self.assertTrue(self.connect())
        attempts.assert_called_once_with('office', 'unit-b')
        self.assertEqual(self.cache()['index'], 1)
        self.assertNotIn('unit-b', Path('.wifi.json').read_text())
        self.wlan.disconnect()
        self.wlan = network.WLAN()  # New boot; only the filesystem preference survives.
        with patch.object(self.wlan, 'scan') as scan, patch('os.rename', wraps=os.rename) as save:
            self.assertTrue(self.connect())
        scan.assert_not_called()
        save.assert_not_called()  # No flash write for an unchanged successful profile.

    def test_lost_preferred_network_triggers_scan_and_updates_preference(self):
        self.assertTrue(self.connect())
        self.wlan.disconnect()
        network.WLAN.available_networks = {'home': 'unit-a'}
        with patch.object(self.wlan, 'scan', wraps=self.wlan.scan) as scan, \
                patch.object(self.wlan, 'connect', wraps=self.wlan.connect) as attempts:
            self.assertTrue(self.connect())
        self.assertEqual([call.args[0] for call in attempts.call_args_list], ['office', 'home'])
        scan.assert_called_once()
        self.assertEqual(self.cache()['index'], 0)

    def test_wrong_password_tries_next_visible_profile(self):
        network.WLAN.available_networks = {'home': 'unit-different', 'office': 'unit-b'}
        with patch.object(self.wlan, 'connect', wraps=self.wlan.connect) as attempts:
            self.assertTrue(self.connect())
        self.assertEqual([call.args[0] for call in attempts.call_args_list], ['home', 'office'])
        self.assertEqual(self.cache()['index'], 1)
        self.assertNotIn('unit-b', self.output.getvalue())

    def test_unavailable_profiles_do_not_overwrite_last_success(self):
        self.assertTrue(self.connect())
        saved = Path('.wifi.json').read_bytes()
        self.wlan.disconnect()
        network.WLAN.available_networks = {}
        self.assertFalse(self.connect())
        self.assertEqual(Path('.wifi.json').read_bytes(), saved)
        self.assertLessEqual(self.timer.elapsed, 8000)

    def test_failed_scan_still_attempts_configured_networks(self):
        with patch.object(self.wlan, 'scan', side_effect=OSError('scan failed')):
            self.assertTrue(self.connect())
        self.assertEqual(self.cache()['index'], 1)

    def test_changed_credentials_and_broken_cache_force_discovery(self):
        self.assertTrue(self.connect())
        old_hash = self.cache()['fingerprint']
        self.wlan.disconnect()
        self.profiles[1]['PASSWD'] = 'unit-new'
        network.WLAN.available_networks = {'office': 'unit-new'}
        with patch.object(self.wlan, 'scan', wraps=self.wlan.scan) as scan:
            self.assertTrue(self.connect())
        scan.assert_called_once()
        self.assertNotEqual(self.cache()['fingerprint'], old_hash)
        self.wlan.disconnect()
        Path('.wifi.json').write_text('{')
        self.assertTrue(self.connect())

    def test_all_wrong_passwords_are_bounded(self):
        network.WLAN.available_networks = {p['SSID']: 'unit-wrong' for p in self.profiles}
        with patch.object(self.wlan, 'connect', wraps=self.wlan.connect) as attempts:
            self.assertFalse(self.connect(timeout=30000))
        self.assertEqual(attempts.call_count, 4)  # Early profiles cannot starve the last one.
        self.assertLessEqual(self.timer.elapsed, 45200)
        self.assertFalse(Path('.wifi.json').exists())

    def test_open_network_and_repeated_ssid_with_other_password(self):
        network.WLAN.available_networks = {'open': ''}
        self.assertTrue(self.connect())
        self.assertEqual(self.cache()['index'], 2)
        self.wlan.disconnect()
        network.WLAN.available_networks = {'home': 'unit-correct'}
        profiles = [{'SSID': 'home', 'PASSWD': 'unit-wrong'}, {'SSID': 'home', 'PASSWD': 'unit-correct'}]
        self.assertTrue(self.connect(profiles))
        self.assertEqual(self.cache()['index'], 1)

    def test_invalid_profiles_fail_without_echoing_passwords(self):
        for profiles in ([], {}, self.profiles + [self.profiles[0]], [self.profiles[0], self.profiles[0]],
                         [{'SSID': 'x', 'PASSWD': True}], [{'SSID': '', 'PASSWD': 'unit-secret'}],
                         [{'SSID': 'ą' * 17, 'PASSWD': 'unit-secret'}],
                         [{'SSID': 'bad\0name', 'PASSWD': 'unit-secret'}],
                         [{'SSID': 'x', 'PASSWD': 'x' * 65}], [{'SSID': 'x', 'PASSWD': 'x', 'extra': 1}]):
            with self.assertRaises(ValueError) as raised:
                self.settings.network_profiles({'WIFI_NETWORKS': profiles})
            self.assertNotIn('unit-secret', str(raised.exception))
        self.assertEqual(self.settings.network_profiles({'WIFI_NETWORKS': self.profiles}), self.profiles)


if __name__ == '__main__':
    unittest.main()
