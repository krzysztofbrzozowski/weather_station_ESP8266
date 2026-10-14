"""Bind station images to current settings, without logging credentials."""
import argparse
import hashlib
import json
from pathlib import Path

from prepare import PROJECT, ROOT, read_settings
from flash import verify_filesystem_image


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_inputs(settings, staging, build):
    config = read_settings(settings)
    if json.loads((staging / 'config.json').read_text()) != config:
        raise ValueError('Staged settings differ from firmware/.settings; rerun ./platformio.sh --build')
    application = build / 'firmware.bin'
    if not application.is_file() or not 4096 < application.stat().st_size < 0x100000:
        raise ValueError('Missing/invalid station firmware.bin')
    verify_filesystem_image(build / 'littlefs.bin', staging)
    return {
        'version': 1, 'environment': 'station', 'settings_sha256': digest(settings),
        'firmware_sha256': digest(application), 'filesystem_sha256': digest(build / 'littlefs.bin'),
        'interval_s': config['MEASUREMENT_INTERVAL_S'],
        'wifi_profiles': len(config.get('WIFI_NETWORKS', [None])),
        'token_entries': config.get('AUTH_TOKENS_COUNT', 1),
    }


def record(settings, staging, build):
    manifest = checked_inputs(settings, staging, build)
    temporary = build / 'deploy-manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(build / 'deploy-manifest.json')
    return manifest


def verify(settings, staging, build):
    saved = json.loads((build / 'deploy-manifest.json').read_text())
    current = checked_inputs(settings, staging, build)
    if saved != current:
        raise ValueError('Settings or images changed since the build; rerun ./platformio.sh --build')
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('record', 'verify'))
    args = parser.parse_args()
    try:
        action = record if args.action == 'record' else verify
        manifest = action(ROOT / 'firmware/.settings', PROJECT / 'data', PROJECT / '.pio/build/station')
    except (ValueError, SyntaxError, OSError) as error:
        parser.exit(1, 'Deployment check failed: {}\n'.format(error))
    print('Verified station images: interval {} s; Wi-Fi profiles {}; token entries {}'.format(
        manifest['interval_s'], manifest['wifi_profiles'], manifest['token_entries']))
    print('Firmware SHA256: ' + manifest['firmware_sha256'])
    print('Filesystem SHA256: ' + manifest['filesystem_sha256'])


if __name__ == '__main__':
    main()
