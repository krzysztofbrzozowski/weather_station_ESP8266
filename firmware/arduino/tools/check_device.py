"""Exercise Arduino HTTPS, real measurements, queue recovery and timed sleep."""
import argparse
from pathlib import Path
import re
import time
import serial

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--boot', action='store_true', help='Wait for startup/service prompt after a physical RST')
    parser.add_argument('--service', action='store_true', help='Send Ctrl+C in the startup service window')
    parser.add_argument('--reset', action='store_true', help='Pulse adapter RTS before waiting for normal boot (SJ2 open)')
    parser.add_argument('--commands', nargs='*', default=['selftest', 'run', 'status'])
    parser.add_argument('--sleep', type=int, help='Test timed wake with the station environment installed')
    parser.add_argument('--sleep-ready-file', type=Path,
                        help='Keep serial open and wait for this file before testing sleep (close SJ2 first)')
    parser.add_argument('--log', type=Path, default=PROJECT / '.pio/hardware-check.log')
    args = parser.parse_args()
    if args.sleep is not None and not 5 <= args.sleep <= 30:
        parser.error('--sleep must be 5-30 seconds')
    port = serial.serial_for_url(args.port, 115200, timeout=0.05, exclusive=True, do_not_open=True)
    port.dtr = False
    port.rts = False
    port.open()
    if args.reset:
        port.rts = True
        time.sleep(0.1)
        port.rts = False
    if args.service:
        port.write(b'\x03')
    log = bytearray()
    pending = bytearray()

    def receive():
        data = port.read(port.in_waiting or 1)
        log.extend(data)
        pending.extend(data)
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending[:] = rest
            print(line.decode(errors='replace').strip(), flush=True)
        return data

    def prompt(timeout=100):
        start = len(log)
        until = time.monotonic() + timeout
        interrupted = False
        while time.monotonic() < until:
            receive()
            output = bytes(log[start:])
            if args.service and not interrupted and (b'Service window:' in output or b'Weather station:' in output):
                port.write(b'\x03')
                interrupted = True
            if b'station> ' in output:
                pending.clear()
                return output
        raise RuntimeError('Service prompt not observed; see saved UART log')

    try:
        print('Serial open with GPIO0 released; waiting for normal boot.' if args.boot else 'Serial open; checking service console.', flush=True)
        if not args.boot:
            port.write(b'\rstatus\r')
        startup = prompt(150 if args.boot else 10)
        if args.boot and not args.service and b'HTTPS probe PASS:' not in startup:
            raise RuntimeError('Boot completed without a successful HTTPS probe')
        for cmd in args.commands:
            print('COMMAND:', cmd, flush=True)
            port.write(cmd.encode() + b'\r')
            output = prompt()
            if cmd == 'selftest' and b'Queue selftest PASS:' not in output:
                raise RuntimeError('Queue recovery/circular-buffer selftest failed')
            if cmd == 'https' and b'HTTPS probe PASS:' not in output:
                raise RuntimeError('HTTPS API time/validation probe failed')
            if cmd == 'run' and b'Queued measurement sent' not in output and b'Measurement sent directly' not in output:
                raise RuntimeError('Real measurement upload was not confirmed')
        if args.sleep is not None:
            if args.sleep_ready_file is not None:
                print('Checks complete; keeping serial open. Close SJ2 before creating the sleep-ready file.', flush=True)
                while not args.sleep_ready_file.exists():
                    receive()
            offset = len(log)
            started = time.monotonic()
            port.write('sleep {}\r'.format(args.sleep).encode())
            until = started + args.sleep + 100
            wake = None
            while time.monotonic() < until:
                receive()
                output = bytes(log[offset:])
                match = re.search(rb'Reset cause: (\d+)', output)
                if match and wake is None:
                    wake = time.monotonic() - started
                    if int(match.group(1)) != 5 or wake < args.sleep - 1:
                        raise RuntimeError('Restart did not match timed deep-sleep wake')
                    print('Timed wake observed after {:.2f}s, reset cause 5.'.format(wake), flush=True)
                if match:
                    cycle = output[match.start():]
                    if re.search(rb'Deep sleep: \d+ ms', cycle):
                        if b'Queued measurement sent' not in cycle:
                            raise RuntimeError('Wake succeeded but upload was not confirmed')
                        print('PASS: timed wake, real POST and next sleep; serial remained open.', flush=True)
                        break
            else:
                raise RuntimeError('Timed wake/cycle not observed within the test window')
        print('PASS: requested hardware checks completed.', flush=True)
    finally:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.log.write_bytes(log)
        port.close()


if __name__ == '__main__':
    main()
