"""Timestamp-only history of this Pico's USB activity, not dock occupancy."""
import json
import os


class DockLog:
    def __init__(self, read_status, directory='.'):
        self.read_status = read_status
        self.path = directory + '/dock-history.jsonl'
        self.previous = directory + '/dock-history.previous.jsonl'
        self.max_bytes = 16384
        self.run = 1
        self.started = False
        self.anchored = False
        self.last = None
        self.candidate = None
        self.at = 0
        self.utc = None
        self.next_poll = 0
        self.error = None
        for path in (self.previous, self.path):
            if self._size(path) > self.max_bytes:
                raise ValueError('USB history exceeds byte budget')
            if not self._size(path):
                continue
            with open(path) as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict) and isinstance(row.get('run'), int):
                            self.run = max(self.run, row['run'] + 1)
                    except ValueError:
                        pass

    def _size(self, path):
        try:
            return os.stat(path)[6]
        except OSError:
            return 0

    def _write(self, event, ms, utc):
        row = {'v': 1, 'run': self.run, 'ms': ms, 'utc': utc, 'event': event}
        # A leading newline isolates any torn final record after a power cut.
        data = '\n' + json.dumps(row) + '\n'
        if len(data) > self.max_bytes:
            raise ValueError('USB history record exceeds byte budget')
        if self._size(self.path) + len(data) > self.max_bytes:
            os.rename(self.path, self.previous)
        with open(self.path, 'a') as f:
            f.write(data)

    def tick(self, now, utc=None):
        if self.error:
            return
        try:
            self._tick(now, utc)
        except Exception as e:
            # Observability must never freeze or reboot the clock.
            self.error = str(e)[:96]

    def _tick(self, now, utc):
        if now < self.next_poll:
            return
        self.next_poll = now + 250
        if not self.started:
            self._write('start', now, utc)
            self.started = True
            self.anchored = utc is not None
        if utc is not None and not self.anchored:
            self._write('time_anchor', now, utc)
            self.anchored = True
        status = self.read_status()
        up = bool(status & 0x10000) and not bool(status & 0x10)
        if up != self.candidate:
            self.candidate, self.at, self.utc = up, now, utc
        if up != self.last and now - self.at >= 1000:
            self._write('up' if up else 'down', self.at, self.utc)
            self.last = up
