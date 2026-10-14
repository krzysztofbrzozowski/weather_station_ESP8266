"""Settings paths must work on ESP8266 builds which omit __file__."""
import importlib.util
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

FIRMWARE = Path(__file__).resolve().parents[1] / 'firmware' / 'micropython' / 'src'
CONFIG = "API_url='http://example.invalid/api'\nAUTH_TOKEN='test'\nBACKUP_FOLDER='backups'\nSSID='wifi'\nPASSWD='password'\n"


def load_module():
    spec = importlib.util.spec_from_file_location('test_station_settings', FIRMWARE / 'settings.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SettingsTest(unittest.TestCase):
    def test_mac_token_selection_and_map_precedence(self):
        module = load_module()
        values = {'AUTH_TOKEN': 'unit-legacy', 'AUTH_TOKENS': {
            'DC-4F-22-7C-3A-FA': 'unit-first', 'dc:4f:22:7c:3a:fc': 'unit-second'}}
        self.assertEqual(module.select_token(values, 'dc:4f:22:7c:3a:fa'), 'unit-first')
        self.assertEqual(module.select_token(values, 'DC:4F:22:7C:3A:FC'), 'unit-second')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(module.select_token(values, 'dc:4f:22:7c:3c:0d'), '')
        self.assertIn('uploads disabled', output.getvalue())
        self.assertNotIn('unit-', output.getvalue())

    def test_shared_settings_loads_without_legacy_token(self):
        module = load_module()
        text = CONFIG.replace("AUTH_TOKEN='test'\n", '')
        text += "AUTH_TOKENS = {'dc:4f:22:7c:3a:fa': 'unit-first', 'dc:4f:22:7c:3a:fc': 'unit-second'}\n"
        with patch('builtins.open', return_value=io.StringIO(text)):
            config = module.load_settings('/.settings', mac='dc:4f:22:7c:3a:fc')
        self.assertEqual(config['AUTH_TOKEN'], 'unit-second')
        self.assertNotIn('AUTH_TOKENS', config)  # Release other tokens before TLS.

    def test_loads_one_hundred_entries_and_selects_first_middle_last(self):
        module = load_module()
        mapping = {'02:00:00:00:00:%02x' % n: 'unit-%03d-' % n + 'x' * 247 for n in range(100)}
        text = CONFIG.replace("AUTH_TOKEN='test'\n", '') + 'AUTH_TOKENS = ' + repr(mapping)
        for n in (0, 50, 99):
            mac = '02:00:00:00:00:%02x' % n
            with patch('builtins.open', return_value=io.StringIO(text)):
                config = module.load_settings('/.settings', mac=mac)
            self.assertEqual(config['AUTH_TOKEN'], mapping[mac])
            self.assertNotIn('AUTH_TOKENS', config)

    def test_device_mac_is_read_from_station_interface(self):
        module = load_module()
        from stubs import network
        from types import SimpleNamespace
        wlan = SimpleNamespace(config=lambda key: bytes.fromhex('dc4f227c3afa'))
        with patch.dict('sys.modules', {'network': network}), patch.object(network, 'WLAN', return_value=wlan) as constructor:
            self.assertEqual(module.select_token({'AUTH_TOKENS': {'dc:4f:22:7c:3a:fa': 'unit-first'}}), 'unit-first')
        constructor.assert_called_once_with(network.STA_IF)

    def test_invalid_token_maps_are_rejected_without_exposing_tokens(self):
        module = load_module()
        for mapping in ({}, [], None, 'unit-secret', {'invalid': 'unit-secret'},
                        {'dc:4f:22:7c:3a:fa': ''}, {'dc:4f:22:7c:3a:fa': True},
                        {'dc:4f:22:7c:3a:fa': 'unit-secret\r\nInjected'},
                        {'dc:4f:22:7c:3a:fa': 'unit-secret', 'DC-4F-22-7C-3A-FA': 'unit-other'},
                        {'02:00:00:00:00:%02x' % n: 'unit-secret' for n in range(101)}):
            with self.subTest(mapping=type(mapping)), self.assertRaises(ValueError) as raised:
                module.select_token({'AUTH_TOKENS': mapping}, 'dc:4f:22:7c:3a:fa')
            self.assertNotIn('unit-secret', str(raised.exception))

    def test_missing_file_attribute_reads_device_root(self):
        module = load_module()
        del module.__file__
        with patch('builtins.open', return_value=io.StringIO(CONFIG)) as opened:
            config = module.load_settings()
        opened.assert_called_once_with('/.settings')
        self.assertEqual(config['BACKUP_FOLDER'], 'backups')
        self.assertEqual(config['BACKUP_MAX_RECORDS'], 144)

    def test_cpython_uses_shared_firmware_settings_with_other_working_directory(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / 'firmware'
            source = firmware / 'micropython' / 'src'
            source.mkdir(parents=True)
            path = firmware / '.settings'
            path.write_text(CONFIG)
            # A stale source-adjacent copy must not override shared settings.
            (source / '.settings').write_text(CONFIG.replace("SSID='wifi'", "SSID='stale'"))
            module.__file__ = str(source / 'settings.py')
            original = Path.cwd()
            try:
                os.chdir(FIRMWARE.parent)
                config = module.load_settings()
            finally:
                os.chdir(original)
        self.assertEqual(config['SSID'], 'wifi')

    def test_micropython_reads_device_root_even_with_file_attribute(self):
        module = load_module()
        module.__file__ = '/settings.py'
        from types import SimpleNamespace
        with patch.object(module.sys, 'implementation', SimpleNamespace(name='micropython')), \
                patch('builtins.open', return_value=io.StringIO(CONFIG)) as opened:
            module.load_settings()
        opened.assert_called_once_with('/.settings')

    def test_explicit_path_works_without_file_attribute(self):
        module = load_module()
        del module.__file__
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.settings'
            path.write_text(CONFIG)
            self.assertEqual(module.load_settings(str(path))['AUTH_TOKEN'], 'test')


if __name__ == '__main__':
    unittest.main()
