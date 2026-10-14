"""Verify real POST/sleep/wake cycles from a continuously open UART."""
import re


class CycleVerifier:
    def __init__(self, target, interval=None):
        self.target = target
        self.interval = interval
        self.completed = 0
        self.pending = bytearray()
        self.accepted = False
        self.reason = None
        self.sleep_at = None
        self.sleep_ms = None
        self.woken = False

    def feed(self, data, now):
        self.pending.extend(data)
        while b'\n' in self.pending:
            raw, _, rest = self.pending.partition(b'\n')
            self.pending[:] = rest
            line = raw.decode(errors='replace').strip()
            if 'Weather station:' in line:
                self.accepted = False
                self.reason = None
            match = re.search(r'Reset cause: (\d+)', line)
            if match:
                self.reason = int(match.group(1))
                if self.sleep_at is not None:
                    elapsed = now - self.sleep_at
                    if self.reason != 5 or elapsed < self.sleep_ms / 1000 - 1:
                        raise RuntimeError('Restart did not match a timed deep-sleep wake (cause {})'.format(self.reason))
                    self.woken = True
                    print('Verified timed wake after {:.2f} s; reset cause 5.'.format(elapsed), flush=True)
            if 'Queued measurement sent' in line or 'Measurement sent directly' in line:
                self.accepted = True
            match = re.fullmatch(r'Deep sleep: (\d+) ms', line)
            if match:
                milliseconds = int(match.group(1))
                if milliseconds < 1000:
                    raise RuntimeError('Invalid sleep duration below one second')
                if not self.accepted or self.reason is None:
                    raise RuntimeError('Cycle entered sleep without a confirmed measurement upload; inspect UART log')
                if self.completed and not self.woken:
                    raise RuntimeError('Another sleep was observed without a verified timed wake')
                if self.interval is not None and milliseconds > self.interval * 1000:
                    raise RuntimeError('Device sleep {} ms exceeds configured interval {} s; settings mismatch'.format(milliseconds, self.interval))
                self.completed += 1
                self.sleep_at, self.sleep_ms, self.woken = now, milliseconds, False
                print('Verified cycle {}/{}: measurement accepted, sleep {} ms.'.format(
                    self.completed, self.target, milliseconds), flush=True)
        if len(self.pending) > 8192:
            raise RuntimeError('Unexpected oversized UART line')

    def check_timeout(self, now):
        if self.sleep_at is not None and not self.woken and self.completed < self.target:
            if now > self.sleep_at + self.sleep_ms / 1000 + 10:
                raise RuntimeError('No automatic wake after sleep; check GPIO16-RST/SJ2 connection and power')

    @property
    def done(self):
        return self.completed >= self.target
