"""Export MicroPython files and stage a private Arduino LittleFS image."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import time
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[1]
DEFAULTS = {
    'MEASUREMENT_INTERVAL_S': (300, 10, 3600), 'HTTP_TIMEOUT_S': (3, 1, 10),
    'BACKUP_MAX_RECORDS': (144, 1, 512), 'BACKUP_MIN_FREE_BYTES': (32768, 8192, 524288),
    'BACKUP_MAX_SEND': (6, 1, 20), 'WIFI_TIMEOUT_S': (12, 1, 30),
    'NTP_TIMEOUT_S': (15, 1, 30),
    'MAX_AWAKE_S': (90, 30, 180),
}


def literal_settings(path):
    values = {}
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ValueError('Settings must contain literal assignments and comments only')
        try:
            values[node.targets[0].id] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            raise ValueError('Settings must contain literal values only') from None
    return values


def read_settings(path):
    values = literal_settings(path)
    required = ('API_url', 'AUTH_TOKEN', 'SSID', 'PASSWD', 'BACKUP_FOLDER')
    for key in required:
        if not isinstance(values.get(key), str):
            raise ValueError('Missing/invalid setting: ' + key)
    url = urlsplit(values['API_url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
            or any(c in values['API_url'] for c in '\r\n')):
        raise ValueError('API_url must be HTTPS without credentials, query or fragment')
    if not values['AUTH_TOKEN'] or any(c in values['AUTH_TOKEN'] for c in '\r\n'):
        raise ValueError('Invalid AUTH_TOKEN')
    folder = values['BACKUP_FOLDER']
    if not folder or len(folder) > 24 or folder in ('.', '..') or '/' in folder or '\\' in folder:
        raise ValueError('Invalid BACKUP_FOLDER')
    if not 1 <= len(values['SSID'].encode()) <= 32 or len(values['PASSWD'].encode()) > 64:
        raise ValueError('Invalid Wi-Fi settings length')
    result = {key: values[key] for key in required}
    for key, (default, low, high) in DEFAULTS.items():
        value = values.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Invalid numeric setting: ' + key)
        result[key] = value
    result['NTP_SERVER'] = values.get('NTP_SERVER', 'time.cloudflare.com')
    result['CA_FILE'] = '/ca.pem'
    if not isinstance(result['NTP_SERVER'], str) or not result['NTP_SERVER']:
        raise ValueError('Invalid NTP_SERVER')
    return result


def export_device(port, wait_seconds):
    from mpremote.transport_serial import SerialTransport
    output = PROJECT / 'device_backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output.mkdir(parents=True, mode=0o700)
    device = SerialTransport(port, timeout=0.02)
    manifest = {'version': 1, 'complete': False, 'files': {}}
    try:
        print('Port open; waiting for reset/wake to export the MicroPython filesystem.', flush=True)
        deadline = time.monotonic() + wait_seconds
        captured = b''
        while time.monotonic() < deadline:
            device.serial.write(b'\r\x03\x03\x01')
            captured = (captured + device.serial.read(device.serial.in_waiting or 1))[-8192:]
            if b'raw REPL; CTRL-B to exit\r\n' in captured:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError('No REPL response; press physical RST while the export is waiting')
        device.enter_raw_repl(soft_reset=True, timeout_overall=2)
        device.serial.timeout = 1
        # Use the conservative raw protocol for long exports on ESP8266.
        device.use_raw_paste = False
        # A software reset may leave the ESP8266 watchdog enabled. Feed it
        # explicitly throughout a filesystem export that can take many seconds.
        device.exec("import machine; _export_wdt = machine.WDT(); _export_wdt.feed()")
        original_exec = device.exec
        def fed_exec(code, data_consumer=None):
            if isinstance(code, bytes):
                code = b'_export_wdt.feed()\n' + code
            else:
                code = '_export_wdt.feed()\n' + code
            return original_exec(code, data_consumer=data_consumer)
        device.exec = fed_exec
        manifest['rtc_memory_hex'] = device.eval("__import__('ubinascii').hexlify(__import__('machine').RTC().memory()).decode()")
        manifest['device_time'] = device.eval("__import__('time').time()")

        def walk(remote, relative):
            device.exec('_export_wdt.feed()')
            entries = device.eval("__import__('os').listdir({!r})".format(remote))
            for name in sorted(entries):
                if name in ('.', '..') or '/' in name or '\\' in name:
                    raise RuntimeError('Invalid remote filename')
                child = remote.rstrip('/') + '/' + name
                rel = relative / name
                device.exec('_export_wdt.feed()')
                mode = device.eval("__import__('os').stat({!r})[0]".format(child))
                if mode & 0x4000:
                    walk(child, rel)
                else:
                    raw = device.fs_readfile(child)
                    device.exec('_export_wdt.feed()')
                    destination = output / 'files' / rel
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.write_bytes(raw)
                    destination.chmod(0o600)
                    manifest['files'][rel.as_posix()] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        walk('/', Path())
        manifest['complete'] = True
        print('Exported {} files to {}'.format(len(manifest['files']), output), flush=True)
        device.exec('_export_wdt.feed()')
    finally:
        manifest_path = output / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        manifest_path.chmod(0o600)
        device.close()
    return output


def verify_export(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest.get('complete') is not True:
        raise ValueError('Incomplete device export')
    for name, expected in manifest['files'].items():
        path = directory / 'files' / name
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Unsafe path in device export')
        raw = path.read_bytes()
        if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
            raise ValueError('Export checksum mismatch: ' + name)
    return manifest


def extract_flash(image, offset=None):
    """Read LittleFS from a snapshot without formatting or writing the image."""
    from littlefs import LittleFS
    from littlefs.context import UserContext
    raw = image.read_bytes()
    if len(raw) != 0x400000:
        raise ValueError('Expected a complete 4 MiB ESP8266 flash snapshot')
    if offset is None:
        candidates = set()
        index = raw.find(b'littlefs', 0x80000)
        while index >= 0:
            try:
                version, block_size, block_count, _, _, _ = struct.unpack_from('<6I', raw, index + 12)
                if version >> 16 == 2 and block_size in (4096, 8192) and 2 <= block_count <= len(raw) // block_size:
                    sector = index // block_size * block_size
                    candidates.update((base, block_size, block_count) for base in (sector, sector - block_size))
            except struct.error:
                pass
            index = raw.find(b'littlefs', index + 8)
        candidates = sorted(p for p in candidates if 0x80000 <= p[0] < 0x3FC000 and p[0] + p[1] * p[2] <= len(raw))
    else:
        candidates = [(offset, 4096, (0x3FC000 - offset) // 4096)]
    filesystem = None
    for candidate, block_size, block_count in candidates:
        attempt = LittleFS(context=UserContext(buffer=bytearray(raw[candidate:candidate + block_size * block_count])), mount=False,
                           block_size=block_size, block_count=block_count, read_size=32, prog_size=32, cache_size=64, lookahead_size=32)
        try:
            attempt.mount()
            with attempt.open('/.settings', 'rb') as f:
                if b'API_url' not in f.read(4096):
                    continue
        except Exception:
            continue
        filesystem = attempt
        offset = candidate
        break
    if filesystem is None:
        raise ValueError('No mountable MicroPython LittleFS found in snapshot')
    output = PROJECT / 'device_backups' / ('flash-export-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    output.mkdir(parents=True, mode=0o700)
    manifest = {'version': 1, 'complete': False, 'files': {}, 'flash_offset': offset,
                'flash_image_sha256': hashlib.sha256(raw).hexdigest()}
    for directory, _, names in filesystem.walk('/'):
        for name in names:
            relative = Path(directory.lstrip('/')) / name
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unsafe path in flash filesystem')
            with filesystem.open('/' + relative.as_posix(), 'rb') as f:
                content = f.read()
            destination = output / 'files' / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(content)
            destination.chmod(0o600)
            manifest['files'][relative.as_posix()] = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    filesystem.unmount()
    manifest['complete'] = True
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify_export(output)
    print('Extracted {} files at flash offset 0x{:x}: {}'.format(len(manifest['files']), offset, output))
    return output


def stage(settings, exported=None, replace=False, output=None):
    config = read_settings(settings)
    target = Path(output) if output is not None else PROJECT / 'data'
    if target.exists() and not replace:
        raise ValueError('Staged data already exists; use --replace to regenerate the host image')
    manifest = verify_export(exported) if exported is not None else None
    ca = PROJECT / 'certs' / 'isrg-roots.pem'
    if not ca.exists():
        raise ValueError('Missing public CA bundle')
    # Validate everything before replacing the generated directory.
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, mode=0o700)
    config_path = target / 'config.json'
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
    config_path.chmod(0o600)
    shutil.copyfile(ca, target / 'ca.pem')
    migrated = 0
    if exported is not None:
        source = exported / 'files'
        # Use the exported device's queue directory even if the local config was
        # edited after the export. Raw settings are never embedded in firmware.
        old_config = literal_settings(source / '.settings')
        old_name = old_config.get('BACKUP_FOLDER')
        if not isinstance(old_name, str) or not old_name or old_name in ('.', '..') or '/' in old_name or '\\' in old_name:
            raise ValueError('Invalid backup directory in device export')
        old_folder = source / old_name
        if old_folder.exists():
            destination = target / config['BACKUP_FOLDER']
            destination.mkdir()
            for file in old_folder.iterdir():
                if file.is_file():
                    shutil.copyfile(file, destination / file.name)
                    (destination / file.name).chmod(0o600)
                    migrated += 1
        (target / '.migration.json').write_text(json.dumps({'export': str(exported.resolve()), 'files': migrated,
            'export_manifest_sha256': hashlib.sha256((exported / 'manifest.json').read_bytes()).hexdigest()}) + '\n')
    print('Private filesystem staged: {}; migrated queue files: {}'.format(target, migrated))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    export = commands.add_parser('export')
    export.add_argument('--port', required=True)
    export.add_argument('--wait-seconds', type=int, default=360)
    extract = commands.add_parser('extract')
    extract.add_argument('--image', type=Path, required=True)
    extract.add_argument('--offset', type=lambda v: int(v, 0))
    prepare = commands.add_parser('stage')
    prepare.add_argument('--settings', type=Path, default=ROOT / 'firmware/.settings')
    prepare.add_argument('--export', type=Path)
    prepare.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'export':
            export_device(args.port, args.wait_seconds)
        elif args.action == 'extract':
            extract_flash(args.image, args.offset)
        else:
            stage(args.settings, args.export, args.replace)
    except (ValueError, OSError, RuntimeError) as error:
        # Never echo raw settings or token-bearing HTTP headers on failure.
        parser.exit(1, 'Preparation failed: {}\n'.format(error))


if __name__ == '__main__':
    main()
