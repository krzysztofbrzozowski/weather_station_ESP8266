"""Back up or flash this 4 MiB ESP8266 with explicit, verifiable image inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import serial
from esptool.targets.esp8266 import ESP8266ROM
from esptool.cmds import attach_flash, write_flash

from prepare import PROJECT, verify_export
from device_cycles import CycleVerifier

FLASH_SIZE = 0x400000
FS_OFFSET = 0x300000
FS_SIZE = 0xFA000


def verify_written_images(esp, images):
    # Check all images again AFTER the last write: a filesystem write must not
    # have damaged the earlier application (for example on misidentified flash).
    for offset, raw in images:
        if offset == 0 and raw[0] == 0xE9:
            # Match the DIO / 40 MHz / 4 MiB header passed to write_flash below.
            raw = raw[:2] + b'\x02\x40' + raw[4:]
        if esp.flash_md5sum(offset, len(raw)).lower() != hashlib.md5(raw).hexdigest():
            raise ValueError('Final flash verification failed at 0x{:06x}; application will not be started'.format(offset))
    print('Final flash check: all written images still match after the last write.', flush=True)


def verify_backup(image):
    metadata = json.loads(image.with_suffix('.json').read_text())
    raw = image.read_bytes()
    if metadata.get('complete') is not True or len(raw) != FLASH_SIZE or hashlib.sha256(raw).hexdigest() != metadata.get('sha256'):
        raise ValueError('Missing/incomplete/corrupted full flash backup')
    return metadata


def verify_filesystem_image(image, staging):
    from littlefs import LittleFS
    from littlefs.context import UserContext
    raw = image.read_bytes()
    if len(raw) != FS_SIZE:
        raise ValueError('Unexpected LittleFS image size for the 4 MiB/1 MiB layout')
    fs = LittleFS(context=UserContext(buffer=bytearray(raw)), mount=False,
                  block_size=8192, block_count=FS_SIZE // 8192, read_size=64, prog_size=64, cache_size=64, lookahead_size=64)
    fs.mount()
    actual = set()
    try:
        for directory, _, names in fs.walk('/'):
            for name in names:
                relative = Path(directory.lstrip('/')) / name
                actual.add(relative.as_posix())
                with fs.open('/' + relative.as_posix(), 'rb') as f:
                    if f.read() != (staging / relative).read_bytes():
                        raise ValueError('Filesystem image does not match staged data')
        expected = {p.relative_to(staging).as_posix() for p in staging.rglob('*') if p.is_file()}
        if actual != expected or not {'config.json', 'ca.pem'}.issubset(actual):
            raise ValueError('Filesystem image is incomplete or stale')
    finally:
        fs.unmount()


def connect(port_name, baud, manual, attempts):
    port = serial.serial_for_url(port_name, baudrate=baud, timeout=0.3, exclusive=True, do_not_open=True)
    port.dtr = manual
    port.rts = False
    port.open()
    if manual:
        print('GPIO0 programming line held active. Enter the bootloader with your board BOOT/RST controls.', flush=True)
    try:
        loader = ESP8266ROM(port)
        loader.connect(mode='no-reset' if manual else 'default-reset', attempts=attempts)
        loader.ESP_RAM_BLOCK = 1024
        esp = loader.run_stub()
        esp.FLASH_WRITE_SIZE = 1024
        # The CLI normally does this. Direct API users must initialize/reset
        # the flash too, especially when connecting after deep sleep.
        attach_flash(esp)
        esp.flash_set_parameters(FLASH_SIZE)
        flash_id = esp.flash_id(cache=False)
        if 1 << ((flash_id >> 16) & 0xff) != FLASH_SIZE:
            raise ValueError('This migration is configured for a 4 MiB flash chip only')
        return port, esp
    except BaseException:
        port.dtr = False
        port.rts = False
        port.close()
        raise


def backup(esp, image):
    image.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if image.exists() or image.with_suffix('.json').exists():
        raise ValueError('Backup destination already exists; choose a new filename')
    partial = image.with_suffix('.partial.bin')
    with partial.open('wb') as f:
        partial.chmod(0o600)
        for offset in range(0, FLASH_SIZE, 0x10000):
            # The stub's read_flash verifies a device MD5 for every block.
            data = esp.read_flash(offset, 0x10000)
            f.write(data)
            f.flush()
            if (offset + len(data)) % 0x40000 == 0:
                print('Verified backup: {}/4096 KiB'.format((offset + len(data)) // 1024), flush=True)
    raw = partial.read_bytes()
    if len(raw) != FLASH_SIZE or hashlib.md5(raw).hexdigest().lower() != esp.flash_md5sum(0, FLASH_SIZE).lower():
        raise ValueError('Full-device backup checksum mismatch')
    partial.rename(image)
    metadata = {'complete': True, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                'device_md5': hashlib.md5(raw).hexdigest(), 'mac': ':'.join('{:02x}'.format(n) for n in esp.read_mac())}
    manifest = image.with_suffix('.json')
    manifest.write_text(json.dumps(metadata, indent=2) + '\n')
    manifest.chmod(0o600)
    print('Full flash backup verified: {}'.format(image), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'write', 'restore'))
    parser.add_argument('--port', required=True)
    parser.add_argument('--backup', type=Path, help='Optional rollback snapshot; required for backup/restore actions')
    parser.add_argument('--environment', choices=('tls_probe', 'station'), default='station',
                        help='Firmware variant to write (default: station; tls_probe is diagnostic only)')
    parser.add_argument('--with-filesystem', action='store_true')
    parser.add_argument('--export', type=Path)
    parser.add_argument('--manual-reset', action='store_true')
    parser.add_argument('--service', action='store_true', help='Request service mode on first boot after writing')
    parser.add_argument('--led-test', action='store_true', help='Enter service mode and test GPIO2 and GPIO1 separately with UART disabled')
    parser.add_argument('--verify-cycles', type=int, default=0, help='Verify 1-5 real measurement/sleep cycles after writing station')
    parser.add_argument('--baud', type=int, default=115200,
                        help='Initial/current loader baud (115200 after physical reset)')
    parser.add_argument('--transfer-baud', type=int, default=115200)
    parser.add_argument('--connect-attempts', type=int, default=20,
                        help='Use 0 to wait until Ctrl+C during manual bootloader entry')
    args = parser.parse_args()
    if args.led_test:
        if args.action != 'write' or args.environment != 'station' or args.verify_cycles:
            parser.error('--led-test requires writing station without --verify-cycles')
        args.service = True
    if not 0 <= args.verify_cycles <= 5 or (args.verify_cycles and
            (args.action != 'write' or args.environment != 'station' or args.service)):
        parser.error('--verify-cycles requires writing station without --service, and accepts 1-5')
    metadata = None
    build = PROJECT / '.pio/build' / args.environment
    if args.action in ('backup', 'restore') and args.backup is None:
        parser.error('--backup is required for this action')
    if args.action != 'backup' and args.backup is not None:
        metadata = verify_backup(args.backup)
    if args.action == 'write':
        application = build / 'firmware.bin'
        if not application.exists() or not 4096 < application.stat().st_size < 0x100000:
            parser.error('Build the selected application before writing')
        print('Selected firmware: {}; image: {}'.format(args.environment, application), flush=True)
        print('Image SHA256: {}'.format(hashlib.sha256(application.read_bytes()).hexdigest()), flush=True)
        if args.with_filesystem:
            if args.export is not None:
                exported = verify_export(args.export)
                if metadata and exported.get('flash_image_sha256') not in (None, metadata['sha256']):
                    parser.error('The extracted export belongs to another flash snapshot')
            verify_filesystem_image(build / 'littlefs.bin', PROJECT / 'data')
    port = None
    try:
        port, esp = connect(args.port, args.baud, args.manual_reset, args.connect_attempts)
        print('Connected device MAC: {}'.format(':'.join('{:02x}'.format(n) for n in esp.read_mac())), flush=True)
        if args.transfer_baud != args.baud:
            esp.change_baud(args.transfer_baud)
        if metadata and metadata.get('mac'):
            actual = ':'.join('{:02x}'.format(n) for n in esp.read_mac())
            if actual != metadata['mac']:
                raise ValueError('The backup belongs to a different device')
        if args.action == 'backup':
            backup(esp, args.backup)
            return
        if args.action == 'restore':
            images = [(0, args.backup.read_bytes())]
        else:
            images = [(0, (build / 'firmware.bin').read_bytes())]
            if args.with_filesystem:
                # Do not replace a filesystem that has changed since its backup.
                if args.export and metadata and esp.flash_md5sum(0, FLASH_SIZE).lower() != metadata['device_md5'].lower():
                    raise ValueError('Device flash changed since backup; export/back up current data before replacing its filesystem')
                images.append((FS_OFFSET, (build / 'littlefs.bin').read_bytes()))
        write_flash(esp, images, flash_mode='dio', flash_freq='40m', flash_size='4MB',
                    compress=True, no_progress=True)
        verify_written_images(esp, images)
        print('Flash write verified; starting application.', flush=True)
        port.dtr = False
        # With SJ2 open, RTS starts a normal boot reliably on this adapter.
        esp.hard_reset()
        port.baudrate = 115200
        port.timeout = 0.1
        expected = json.loads((PROJECT / 'data/config.json').read_text()) if args.action == 'write' and args.with_filesystem else {}
        interval = expected.get('MEASUREMENT_INTERVAL_S')
        verifier = CycleVerifier(args.verify_cycles, interval) if args.verify_cycles else None
        awake_budget = max(65, expected.get('MAX_AWAKE_S', 90) + 15)
        deadline = time.monotonic() + (awake_budget if verifier else 65)
        log = bytearray()
        interrupted = False
        led_test_sent = False
        led_test_start = 0
        try:
            while time.monotonic() < deadline:
                data = port.read(port.in_waiting or 1)
                log.extend(data)
                if data:
                    print(data.decode(errors='replace'), end='', flush=True)
                if verifier:
                    previous_cycles = verifier.completed
                    verifier.feed(data, time.monotonic())
                    verifier.check_timeout(time.monotonic())
                    if verifier.done:
                        print('PASS: verified {} measurement/sleep cycles.'.format(verifier.completed), flush=True)
                        break
                    if verifier.completed != previous_cycles:
                        deadline = time.monotonic() + verifier.sleep_ms / 1000 + awake_budget
                else:
                    if args.service and not interrupted and (b'Service window:' in log or b'Weather station:' in log):
                        port.write(b'\x03')
                        interrupted = True
                    if args.led_test and not led_test_sent and b'station> ' in log:
                        led_test_start = len(log)
                        port.write(b'ledtest\r')
                        led_test_sent = True
                    if args.led_test:
                        if b'LED test complete:' in log[led_test_start:] and led_test_sent:
                            print('LED test finished. Device remains awake in service mode; reset it to resume measurements.', flush=True)
                            break
                    elif b'station> ' in log or b'Deep sleep:' in log:
                        break
            if verifier and not verifier.done:
                raise RuntimeError('Startup/cycle verification timed out; inspect first-boot.log')
            if args.led_test and b'LED test complete:' not in log[led_test_start:]:
                raise RuntimeError('LED test did not complete; inspect first-boot.log')
        finally:
            (build / 'first-boot.log').write_bytes(log)
    finally:
        if port is not None:
            port.rts = False
            port.dtr = False
            port.close()


if __name__ == '__main__':
    main()
