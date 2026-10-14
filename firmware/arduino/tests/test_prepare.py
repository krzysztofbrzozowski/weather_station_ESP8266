import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('arduino_prepare', Path(__file__).resolve().parents[1] / 'tools/prepare.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = self.root / '.settings'
        self.settings.write_text("API_url = 'https://example.com/api/measurements'\nAUTH_TOKEN = 'test-only'\n"
                                 "SSID = 'test-wifi'\nPASSWD = 'test-password'\nBACKUP_FOLDER = 'backup_logs'\n")
        self.output = self.root / 'staged'
        self.export = self.root / 'export'
        self.files = self.export / 'files'
        self.files.mkdir(parents=True)

    def manifest(self, complete=True):
        files = {}
        for path in self.files.rglob('*'):
            if path.is_file():
                raw = path.read_bytes()
                files[path.relative_to(self.files).as_posix()] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        (self.export / 'manifest.json').write_text(json.dumps({'version': 1, 'complete': complete, 'files': files}))

    def test_literal_settings_preserve_escaped_password_and_interval(self):
        with self.settings.open('a') as f:
            f.write("PASSWD = 'quote\\\'and\\\\slash'\nMEASUREMENT_INTERVAL_S = 1800\n")
        values = prepare.read_settings(self.settings)
        self.assertEqual(values['PASSWD'], "quote'and\\slash")
        self.assertEqual(values['MEASUREMENT_INTERVAL_S'], 1800)

    def test_token_map_stages_all_devices_without_legacy_fallback(self):
        with self.settings.open('a') as f:
            f.write("AUTH_TOKENS = {'DC-4F-22-7C-3A-FA': 'unit-first', 'dc:4f:22:7c:3a:fc': 'unit-second'}\n")
        prepare.stage(self.settings, output=self.output)
        config = json.loads((self.output / 'config.json').read_text())
        self.assertEqual(config['AUTH_TOKENS'], {'dc:4f:22:7c:3a:fa': 'unit-first', 'dc:4f:22:7c:3a:fc': 'unit-second'})
        self.assertNotIn('AUTH_TOKEN', config)
        self.settings.write_text(self.settings.read_text().replace("AUTH_TOKEN = 'test-only'\n", ''))
        self.assertEqual(prepare.read_settings(self.settings)['AUTH_TOKENS'], config['AUTH_TOKENS'])

    def test_bad_maps_are_rejected_before_replacing_staged_configuration(self):
        original = self.settings.read_text()
        prepare.stage(self.settings, output=self.output)
        before = (self.output / 'config.json').read_bytes()
        for mapping in ({}, [], None, 'unit-secret', {'invalid': 'unit-secret'},
                        {'dc:4f:22:7c:3a:fa': ''}, {'dc:4f:22:7c:3a:fa': True},
                        {'dc:4f:22:7c:3a:fa': 'unit-secret\r\nInjected'},
                        {'dc:4f:22:7c:3a:fa': 'unit-secret', 'DC-4F-22-7C-3A-FA': 'unit-other'},
                        {'02:00:00:00:00:%02x' % n: 'unit-secret' for n in range(101)}):
            self.settings.write_text(original + 'AUTH_TOKENS = ' + repr(mapping) + '\n')
            with self.assertRaises(ValueError) as raised:
                prepare.stage(self.settings, replace=True, output=self.output)
            self.assertNotIn('unit-secret', str(raised.exception))
            self.assertEqual((self.output / 'config.json').read_bytes(), before)

    def test_one_hundred_maximum_length_tokens_fit_the_device_image(self):
        mapping = {'02:00:00:00:00:%02x' % n: 'unit-%03d-' % n + 'x' * 247 for n in range(100)}
        self.assertTrue(all(len(token) == 256 for token in mapping.values()))
        with self.settings.open('a') as f:
            f.write('AUTH_TOKENS = ' + repr(mapping) + '\n')
        prepare.stage(self.settings, output=self.output)
        raw = (self.output / 'config.json').read_bytes()
        config = json.loads(raw)
        self.assertEqual(config['AUTH_TOKENS_COUNT'], 100)
        self.assertEqual(config['AUTH_TOKENS'], mapping)
        self.assertLessEqual(len(raw), 32768)
        self.assertNotIn('AUTH_TOKEN', config)

    def test_four_wifi_profiles_stage_without_legacy_credentials(self):
        profiles = [{'SSID': 'unit-%d' % n, 'PASSWD': 'unit-password-%d' % n} for n in range(4)]
        self.settings.write_text(self.settings.read_text().replace("SSID = 'test-wifi'\nPASSWD = 'test-password'\n", '') +
                                 'WIFI_NETWORKS = ' + repr(profiles))
        prepare.stage(self.settings, output=self.output)
        config = json.loads((self.output / 'config.json').read_text())
        self.assertEqual(config['WIFI_NETWORKS'], profiles)
        self.assertNotIn('SSID', config)
        self.assertNotIn('PASSWD', config)

    def test_invalid_wifi_profiles_preserve_staged_configuration(self):
        original = self.settings.read_text()
        prepare.stage(self.settings, output=self.output)
        before = (self.output / 'config.json').read_bytes()
        for profiles in ([], {}, [{'SSID': 'x', 'PASSWD': 'unit-secret'}] * 5,
                         [{'SSID': '', 'PASSWD': 'unit-secret'}], [{'SSID': 'ą' * 17, 'PASSWD': 'unit-secret'}],
                         [{'SSID': 'x', 'PASSWD': 'x' * 65}], [{'SSID': 'bad\0ssid', 'PASSWD': 'unit-secret'}],
                         [{'SSID': 'x', 'PASSWD': True}], [{'SSID': 'x', 'PASSWD': 'unit-secret'}] * 2):
            self.settings.write_text(original + 'WIFI_NETWORKS = ' + repr(profiles))
            with self.assertRaises(ValueError) as raised:
                prepare.stage(self.settings, replace=True, output=self.output)
            self.assertNotIn('unit-secret', str(raised.exception))
            self.assertEqual((self.output / 'config.json').read_bytes(), before)

    def test_executable_configuration_is_rejected_without_running_it(self):
        sentinel = self.root / 'must-not-exist'
        with self.settings.open('a') as f:
            f.write("AUTH_TOKEN = __import__('pathlib').Path({!r}).touch()\n".format(str(sentinel)))
        with self.assertRaises(ValueError):
            prepare.read_settings(self.settings)
        self.assertFalse(sentinel.exists())

    def test_insecure_url_and_header_injection_are_rejected(self):
        original = self.settings.read_text()
        for bad in (original.replace('https://', 'http://'), original.replace('test-only', 'token\\r\\nInjected: yes')):
            self.settings.write_text(bad)
            with self.assertRaises(ValueError):
                prepare.read_settings(self.settings)

    def test_invalid_boolean_and_out_of_range_interval_are_rejected(self):
        original = self.settings.read_text()
        for value in ('True', '0', '3601'):
            self.settings.write_text(original + 'MEASUREMENT_INTERVAL_S = ' + value + '\n')
            with self.assertRaises(ValueError):
                prepare.read_settings(self.settings)

    def test_export_checksum_detects_tampering(self):
        (self.files / '.settings').write_bytes(self.settings.read_bytes())
        self.manifest()
        (self.files / '.settings').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            prepare.verify_export(self.export)

    def test_incomplete_export_does_not_replace_staged_files(self):
        self.manifest(complete=False)
        self.output.mkdir()
        sentinel = self.output / 'keep'
        sentinel.write_text('existing staged data')
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            prepare.stage(self.settings, self.export, replace=True, output=self.output)
        self.assertEqual(sentinel.read_text(), 'existing staged data')

    def test_migration_preserves_original_timestamp_rejection_and_clock_mapping(self):
        (self.files / '.settings').write_bytes(self.settings.read_bytes())
        queue = self.files / 'backup_logs'
        queue.mkdir()
        record = {'version': 1, 'data': {'mac_address': 'aa:bb:cc:dd:ee:ff', 'temperature': 21,
                  'timestamp': '2026-10-14T12:00:00Z'}, 'rejected': 400}
        (queue / 'q0000000001.json').write_text(json.dumps(record))
        mapping = {'clock': 'old-session', 'offset': 1760000000}
        (queue / '.clock.json').write_text(json.dumps(mapping))
        self.manifest()
        prepare.stage(self.settings, self.export, output=self.output)
        self.assertEqual(json.loads((self.output / 'backup_logs/q0000000001.json').read_text()), record)
        self.assertEqual(json.loads((self.output / 'backup_logs/.clock.json').read_text()), mapping)
        self.assertFalse((self.output / '.settings').exists())
        self.assertEqual(json.loads((self.output / 'config.json').read_text())['AUTH_TOKEN'], 'test-only')

    def test_existing_staged_files_require_explicit_replace(self):
        prepare.stage(self.settings, output=self.output)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            prepare.stage(self.settings, output=self.output)

    def test_flash_snapshot_extract_preserves_files_without_modifying_image(self):
        from littlefs import LittleFS
        from littlefs.context import UserContext
        fs = LittleFS(context=UserContext(buffsize=4096 * 32), mount=False,
                      block_size=4096, block_count=32, read_size=32, prog_size=32, cache_size=64, lookahead_size=32)
        fs.format()
        fs.mount()
        with fs.open('/.settings', 'wb') as f:
            f.write(self.settings.read_bytes())
        fs.mkdir('/backup_logs')
        measurement = b'{"version":1,"data":{"temperature":21,"mac_address":"aa:bb:cc:dd:ee:ff"}}'
        with fs.open('/backup_logs/q0000000001.json', 'wb') as f:
            f.write(measurement)
        fs.unmount()
        image = self.root / 'flash.bin'
        offset = 0xA0000
        flash = bytearray(b'\xff' * 0x400000)
        flash[offset:offset + len(fs.context.buffer)] = fs.context.buffer
        image.write_bytes(flash)
        before = hashlib.sha256(image.read_bytes()).digest()
        with patch.object(prepare, 'PROJECT', self.root):
            exported = prepare.extract_flash(image)
        self.assertEqual(hashlib.sha256(image.read_bytes()).digest(), before)
        self.assertEqual((exported / 'files/backup_logs/q0000000001.json').read_bytes(), measurement)
        self.assertTrue(prepare.verify_export(exported)['complete'])


if __name__ == '__main__':
    unittest.main()
