"""Deployment must not continue with stale settings or after a failed build."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / 'firmware/arduino/tools'
with patch.object(sys, 'path', [str(TOOLS)] + sys.path):
    import deploy_manifest as deployment
    from device_cycles import CycleVerifier
    import prepare


class ShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='station deploy ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copyfile(ROOT / 'platformio.sh', self.root / 'platformio.sh')
        executable = self.root / '.venv/bin/python'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/bash\nexec "$DEPLOY_TEST_PYTHON" "$DEPLOY_TEST_SHIM" "$@"\n')
        executable.chmod(0o700)
        self.log = self.root / 'calls.jsonl'
        shim = self.root / 'fake_python.py'
        shim.write_text('import json, os, sys\n'
                        'with open(os.environ["DEPLOY_TEST_LOG"], "a") as f:\n'
                        '    f.write(json.dumps({"args":sys.argv[1:], "cwd":os.getcwd()}) + "\\n")\n'
                        'fail = os.environ.get("DEPLOY_TEST_FAIL", "")\n'
                        'if fail and any(fail in arg for arg in sys.argv[1:]): sys.exit(7)\n')
        self.env = dict(os.environ, DEPLOY_TEST_PYTHON=sys.executable, DEPLOY_TEST_SHIM=str(shim),
                        DEPLOY_TEST_LOG=str(self.log))

    def run_script(self, *args):
        return subprocess.run(['bash', str(self.root / 'platformio.sh'), *args],
                              cwd='/tmp', env=self.env, text=True, capture_output=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_build_uses_its_own_checkout_and_station_and_never_flashes(self):
        result = self.run_script('--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[0]['args'][0].endswith('/prepare.py'))
        self.assertIn('buildfs', calls[2]['args'])
        self.assertEqual(calls[3]['args'][-1], 'record')
        self.assertTrue(all(Path(c['cwd']).resolve() == self.root.resolve() for c in calls))
        for c in calls[1:3]:
            self.assertEqual(c['args'][c['args'].index('-e') + 1], 'station')

    def test_flash_rebuilds_verifies_and_uploads_both_images(self):
        result = self.run_script('--flash', '--port', '/dev/fake', '--cycles', '3')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[-2]['args'][-1], 'verify')
        args = calls[-1]['args']
        self.assertTrue(args[0].endswith('/flash.py'))
        self.assertEqual(args[args.index('--environment') + 1], 'station')
        self.assertEqual(args[args.index('--verify-cycles') + 1], '3')
        self.assertIn('--with-filesystem', args)

    def test_settings_or_build_failure_cannot_flash_old_images(self):
        for failing in ('prepare.py', 'platformio'):
            self.env['DEPLOY_TEST_FAIL'] = failing
            self.log.unlink(missing_ok=True)
            result = self.run_script('--flash')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(c['args'][0].endswith('/flash.py') for c in self.calls()))

    def test_code_only_preserves_device_filesystem(self):
        result = self.run_script('--flash', '--code-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--with-filesystem', self.calls()[-1]['args'])


class ManifestTest(unittest.TestCase):
    def setUp(self):
        from littlefs import LittleFS
        from littlefs.context import UserContext
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = root / '.settings'
        self.settings.write_text("API_url='https://example.com/api/measurements'\nAUTH_TOKEN='unit-secret'\n"
                                 "SSID='unit-wifi'\nPASSWD='unit-password'\nBACKUP_FOLDER='backup_logs'\nMEASUREMENT_INTERVAL_S=30\n")
        self.staging, self.build = root / 'data', root / 'build'
        self.build.mkdir()
        with redirect_stdout(io.StringIO()):
            prepare.stage(self.settings, output=self.staging)
        fs = LittleFS(context=UserContext(buffsize=0xFA000), mount=False, block_size=8192,
                      block_count=125, read_size=64, prog_size=64, cache_size=64, lookahead_size=64)
        fs.format()
        fs.mount()
        for file in self.staging.iterdir():
            with fs.open('/' + file.name, 'wb') as output:
                output.write(file.read_bytes())
        fs.unmount()
        (self.build / 'littlefs.bin').write_bytes(bytes(fs.context.buffer))
        (self.build / 'firmware.bin').write_bytes(b'\xe9' * 5000)

    def test_bundle_is_bound_to_settings_and_both_images_without_credentials(self):
        result = deployment.record(self.settings, self.staging, self.build)
        self.assertEqual(result['interval_s'], 30)
        self.assertEqual(deployment.verify(self.settings, self.staging, self.build), result)
        self.assertNotIn('unit-secret', (self.build / 'deploy-manifest.json').read_text())

    def test_changed_source_settings_are_rejected(self):
        deployment.record(self.settings, self.staging, self.build)
        self.settings.write_text(self.settings.read_text().replace('INTERVAL_S=30', 'INTERVAL_S=1800'))
        with self.assertRaises(ValueError):
            deployment.verify(self.settings, self.staging, self.build)

    def test_modified_firmware_is_rejected(self):
        deployment.record(self.settings, self.staging, self.build)
        (self.build / 'firmware.bin').write_bytes(b'\xe8' * 5000)
        with self.assertRaises(ValueError):
            deployment.verify(self.settings, self.staging, self.build)


class CyclesTest(unittest.TestCase):
    def setUp(self):
        self.capture = redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def first_cycle(self, verifier):
        verifier.feed(b'Weather station: Arduino/BearSSL\nReset cause: 6\nQueued measurement sent\nDeep sleep: 14000 ms\n', 10)

    def test_three_real_cycles_require_timed_wakes(self):
        verifier = CycleVerifier(3, 30)
        self.first_cycle(verifier)
        for wake, sleep in ((24.2, 40.2), (54.4, 70.4)):
            verifier.feed(b'Weather station: Arduino/BearSSL\nReset cause: 5\nQueued measurement sent\n', wake)
            verifier.feed(b'Deep sleep: 14000 ms\n', sleep)
        self.assertTrue(verifier.done)

    def test_stale_device_interval_and_unconfirmed_upload_are_rejected(self):
        for transcript in (b'Reset cause: 6\nQueued measurement sent\nDeep sleep: 1782347 ms\n',
                           b'Reset cause: 6\nDeep sleep: 14000 ms\n'):
            with self.assertRaises(RuntimeError):
                CycleVerifier(1, 30).feed(transcript, 10)

    def test_external_reset_or_missing_wake_cannot_pass(self):
        verifier = CycleVerifier(2, 30)
        self.first_cycle(verifier)
        with self.assertRaises(RuntimeError):
            verifier.feed(b'Weather station: Arduino/BearSSL\nReset cause: 6\n', 24.2)
        verifier = CycleVerifier(2, 30)
        self.first_cycle(verifier)
        with self.assertRaises(RuntimeError):
            verifier.check_timeout(35)


if __name__ == '__main__':
    unittest.main()
