"""Versioned, transactional UI preferences shared by both desktop processes."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULTS = {'theme': 'light', 'developer_mode': False, 'follow_logs': True,
            'font_size': 10, 'remember_window': True, 'director_size': '', 'trainer_size': '',
            'auto_check_updates': True, 'update_channel': 'stable', 'auto_report_diagnostics': True}


def valid(key, value):
    if key == 'update_channel':
        return isinstance(value, str) and value in ('stable', 'beta')
    if key == 'theme':
        return isinstance(value, str) and value in ('dark', 'light')
    if key == 'font_size':
        return type(value) is int and 9 <= value <= 14
    if key in ('director_size', 'trainer_size'):
        if value == '':
            return True
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{3,5}x[0-9]{3,5}', value):
            return False
        width, height = map(int, value.split('x'))
        return 320 <= width <= 16384 and 240 <= height <= 16384
    return key in DEFAULTS and type(value) is bool


class Preferences:
    def __init__(self, root):
        self.path = Path(root) / 'ui-preferences.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError('界面偏好来自更新版本，请使用兼容客户端')
            if version == 0:
                db.execute('PRAGMA user_version=1')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def read(self):
        result = dict(DEFAULTS)
        with self.connect() as db:
            for key, value in db.execute('SELECT key,value FROM preferences'):
                if key in DEFAULTS:
                    try:
                        decoded = json.loads(value)
                    except (ValueError, TypeError):
                        continue
                    if valid(key, decoded):
                        result[key] = decoded
        return result

    def update(self, **changes):
        for key, value in changes.items():
            if not valid(key, value):
                raise ValueError('Invalid preference: '+key)
        with self.connect() as db:
            db.executemany('INSERT INTO preferences VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           [(key, json.dumps(value)) for key, value in changes.items()])
        return self.read()
