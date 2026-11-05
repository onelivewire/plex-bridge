"""Private, bounded append-only diagnostic archives; no raw log records."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid


class HistoryArchive:
    SEGMENT_BYTES = 1024 * 1024
    LIMITS = {'details': (7, 80 * 1024 * 1024), 'incidents': (30, 20 * 1024 * 1024)}

    def __init__(self, directory, validate, clock=time.time):
        self.directory = Path(directory)
        self.validate = validate
        self.clock = clock
        self.active = {}
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)

    @staticmethod
    def lane(row):
        return 'incidents' if (row.get('severity') in {'WARNING', 'ERROR', 'CRITICAL'}
            or row.get('outcome') == 'failed'
            or row.get('event') in {'service_start', 'setup_ready', 'registration_failed',
                'registration_refreshed', 'playback_unavailable', 'playback_recovered'}
            or row.get('action') == 'restart') else 'details'

    def files(self, lane):
        return sorted((p for p in self.directory.glob(lane+'-*.jsonl')
                       if p.is_file() and not p.is_symlink()),
                      key=lambda p: (p.stat().st_mtime_ns, p.name))

    def prune(self):
        for lane, (days, budget) in self.LIMITS.items():
            files = self.files(lane)
            total = sum(p.stat().st_size for p in files)
            for p in files:
                # Names carry UTC days; keep the whole boundary day. Rows are
                # additionally age-filtered on read for exact retention.
                day = datetime.strptime(p.name.split('-')[1], '%Y%m%d').replace(tzinfo=timezone.utc).timestamp()
                if total <= budget and day + 86400 >= self.clock() - days*86400:
                    continue
                size = p.stat().st_size
                p.unlink()
                total -= size
                if self.active.get(lane) == p:
                    self.active.pop(lane, None)

    def append(self, rows):
        for raw in rows:
            row = self.validate(raw)
            if row is None:
                continue
            lane = self.lane(row)
            day = row['timestamp'][:10].replace('-', '')
            data = (json.dumps(row, separators=(',', ':'))+'\n').encode()
            if len(data) > 8192:
                continue
            path = self.active.get(lane)
            if (path is None or not path.exists() or day not in path.name
                    or path.stat().st_size + len(data) > self.SEGMENT_BYTES):
                path = self.directory / (lane+'-'+day+'-'+uuid.uuid4().hex+'.jsonl')
                self.active[lane] = path
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'ab') as handle:
                handle.write(data)
        self.prune()

    def read(self, start='', end='', limit=2000):
        """Read an explicit UTC time window; the latest `limit` rows are returned."""
        import heapq
        if not 1 <= limit <= 10000:
            raise ValueError("history limit must be between 1 and 10000")
        found = 0
        rows = []
        for lane, (days, _) in self.LIMITS.items():
            for path in self.files(lane):
                with path.open('rb') as handle:
                    while True:
                        line = handle.readline(8193)
                        if not line:
                            break
                        if len(line) > 8192:
                            # Skip a malformed long record without allocating it.
                            while line and not line.endswith(b'\n'):
                                line = handle.readline(8193)
                            continue
                        try:
                            row = self.validate(json.loads(line))
                        except (ValueError, UnicodeError):
                            continue
                        if row is None:
                            continue
                        stamp = row['timestamp']
                        age = self.clock() - datetime.fromisoformat(stamp).timestamp()
                        if age > days*86400 or (start and stamp < start) or (end and stamp > end):
                            continue
                        found += 1
                        entry = (stamp, found, row)
                        if len(rows) < limit:
                            heapq.heappush(rows, entry)
                        else:
                            heapq.heappushpop(rows, entry)
        return {'events': [entry[2] for entry in sorted(rows)],
                'matched': found, 'truncated': found > limit}

    def summary(self):
        return {lane: {'target_days': days, 'max_bytes': budget,
                       'bytes': sum(p.stat().st_size for p in self.files(lane))}
                for lane, (days, budget) in self.LIMITS.items()}
