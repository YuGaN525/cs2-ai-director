"""Independent, transactional cache and expiring cross-process update claims."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class UpdateState:
    def __init__(self, root, clock=time.time):
        self.path = Path(root) / 'client-updates.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('Unsupported update cache version')
            db.execute('CREATE TABLE IF NOT EXISTS states (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY, owner TEXT NOT NULL, started REAL NOT NULL, expires REAL NOT NULL)')
            if not version:
                db.execute('PRAGMA user_version=1')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key(origin, channel):
        from client_update import CHANNELS, public_origin
        if channel not in CHANNELS:
            raise ValueError('Invalid update channel')
        return public_origin(origin) + '/' + channel

    def read(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM states WHERE key=?', (key,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def merge(self, key, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM states WHERE key=?', (key,)).fetchone()
            try:
                value = json.loads(row[0]) if row else {}
                if not isinstance(value, dict):
                    value = {}
            except (ValueError, TypeError):
                value = {}
            value.update(changes)
            db.execute('INSERT OR REPLACE INTO states VALUES (?,?)', (key, json.dumps(value)))
        return value

    def claim(self, key, duration=15):
        if not 0 < duration <= 300:
            raise ValueError('Invalid claim duration')
        now, owner = self.clock(), uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT started,expires FROM claims WHERE key=?', (key,)).fetchone()
            if row and row[0] <= now < row[1] and row[1] - row[0] <= 300:
                return None
            db.execute('INSERT OR REPLACE INTO claims VALUES (?,?,?,?)', (key, owner, now, now+duration))
        return owner

    def release(self, key, owner):
        with self.connect() as db:
            db.execute('DELETE FROM claims WHERE key=? AND owner=?', (key, owner))

    def reminder(self, key, target, window):
        """Only one live window owns a non-modal reminder for a target per day."""
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM states WHERE key=?', (key,)).fetchone()
            try:
                value = json.loads(row[0]) if row else {}
                if not isinstance(value, dict): value = {}
            except (ValueError, TypeError):
                value = {}
            ignored = value.get('ignored') == target
            since, until = value.get('reminder_at', 0), value.get('reminder_until', 0)
            active = (type(since) in (int,float) and type(until) in (int,float)
                      and since <= now < until and until-since <= 86400
                      and value.get('reminder_target') == target)
            if ignored or active:
                return {'ignored': ignored, 'remind': not ignored and value.get('reminder_window') == window}
            value.update(reminder_target=target, reminder_at=now, reminder_until=now+86400,
                         reminder_window=window)
            db.execute('INSERT OR REPLACE INTO states VALUES (?,?)', (key, json.dumps(value)))
            return {'ignored': False, 'remind': True}

    def complete(self, key, owner, **changes):
        """An expired/old claim cannot publish a late response over its successor."""
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            claim = db.execute('SELECT owner,started,expires FROM claims WHERE key=?', (key,)).fetchone()
            if not claim or claim[0] != owner or not claim[1] <= now < claim[2]:
                return False
            row = db.execute('SELECT value FROM states WHERE key=?', (key,)).fetchone()
            try:
                value = json.loads(row[0]) if row else {}
                if not isinstance(value, dict): value = {}
            except (ValueError, TypeError):
                value = {}
            value.update(changes)
            db.execute('INSERT OR REPLACE INTO states VALUES (?,?)', (key, json.dumps(value)))
            db.execute('DELETE FROM claims WHERE key=? AND owner=?', (key, owner))
        return True
