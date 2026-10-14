"""Settings paths must work on ESP8266 builds which omit __file__."""
import importlib.util
import io
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
