"""Host-side sleep test: keep one serial connection open and verify reset cause."""
import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import time

from mpremote.transport_serial import SerialTransport
from mpremote.transport import TransportError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--seconds', type=int, default=10)
    parser.add_argument('--wait-for-reset', type=int, default=0,
                        help='Seconds to wait for a manual reset or natural wake before testing')
    parser.add_argument('--upload-main', type=Path, help='Atomically install this main.py before testing')
    parser.add_argument('--log', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'firmware/micropython/build/sleep-check.log')
    args = parser.parse_args()
    if not 5 <= args.seconds <= 30:
        parser.error('--seconds must be between 5 and 30')
    transport = SerialTransport(args.port, timeout=0.2)
    log = ''
    try:
        if args.wait_for_reset:
            print('Serial connection open; waiting for reset/wake before arming the sleep test.', flush=True)
            until = time.monotonic() + args.wait_for_reset
            while True:
                try:
                    with redirect_stdout(io.StringIO()):
                        transport.enter_raw_repl(timeout_overall=1)
                    break
                except TransportError:
                    if time.monotonic() >= until:
                        raise SystemExit('INCONCLUSIVE: device did not enter REPL; manual reset required.')
                    time.sleep(0.1)
        else:
            transport.enter_raw_repl(timeout_overall=10)
        if args.upload_main:
            transport.fs_writefile('main.py.tmp', args.upload_main.read_bytes())
            transport.exec("import os; os.rename('main.py.tmp', 'main.py')")
            print('Diagnostic main.py uploaded', flush=True)
        output, errors = transport.exec_raw(
            "import machine; print('EXPECTED', machine.DEEPSLEEP_RESET)", timeout=5)
        if errors:
            raise RuntimeError(errors.decode())
        expected = int(re.search(rb'EXPECTED (\d+)', output).group(1))
        # No close/reopen and no DTR/RTS changes after this command begins.
        started = time.monotonic()
        transport.exec_raw_no_follow(
            "import main; print('SLEEP_ARMED'); main.deep_sleep({})".format(args.seconds * 1000))
        # A normal cycle can use 8s Wi-Fi + HTTP timeouts + a 20s queue budget.
        deadline = started + args.seconds + 45
        wake_time = None
        cause = None
        pending = ''
        wake_position = None
        while time.monotonic() < deadline:
            data = transport.serial.read(transport.serial.in_waiting or 1)
            if not data:
                continue
            text = data.decode('utf-8', errors='replace')
            log += text
            pending += text
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                markers = ('Reset cause:', 'Measurement', 'Charging input', 'Wi-Fi',
                           'Time sync', 'Backup', 'Queued measurement sent', 'Deep sleep:',
                           'Watchdog', 'Rejected', 'sent to server', 'SLEEP_ARMED')
                positions = [line.find(marker) for marker in markers if marker in line]
                if positions:
                    print('{:.2f}s {}'.format(time.monotonic() - started,
                                             line[min(positions):].strip()), flush=True)
            if 'Traceback (most recent call last)' in log:
                raise SystemExit('FAIL: firmware raised an unhandled exception; see saved log.')
            match = re.search(r'Reset cause: (\d+)', log)
            if match and wake_time is None:
                wake_time = time.monotonic() - started
                cause = int(match.group(1))
                wake_position = match.start()
                print('Boot observed after {:.2f}s; reset cause {} (deep sleep = {})'.format(
                    wake_time, cause, expected), flush=True)
                if 'SLEEP_ARMED' not in log or wake_time < args.seconds - 1 or cause != expected:
                    raise SystemExit('FAIL: restart did not match timed deep-sleep wake.')
            if wake_time is not None:
                cycle = log[wake_position:]
                if re.search(r'Deep sleep: \d+ ms', cycle):
                    if 'Queued measurement sent' in cycle or 'sent to server' in cycle:
                        print('PASS: timed wake, measurement upload, and next sleep; serial stayed open.')
                        return
                    raise SystemExit('Wake confirmed; cycle ended without a confirmed upload. See log above.')
        if 'SLEEP_ARMED' not in log:
            raise SystemExit('INCONCLUSIVE: sleep command was not confirmed.')
        if wake_time is None:
            raise SystemExit('FAIL: sleep command ran, but no automatic boot was observed within {}s.'.format(args.seconds + 45))
        raise SystemExit('Wake confirmed, but measurement upload was not confirmed.')
    finally:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.log.write_text(log)
        print('Serial log saved to {}'.format(args.log), flush=True)
        # Closing may affect reset lines; it happens only AFTER the observation.
        transport.close()


if __name__ == '__main__':
    main()
