"""Bounded durable, owner-bound diagnostic outbox. Contains only projected data."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from diagnostics_privacy import SUMMARIES, sanitize_log, utc_now, MAX_LOG_BYTES

MAX_ITEMS, MAX_BYTES, TTL = 20, 4 * 1024 * 1024, 30 * 86400


def binding_key(binding):
    if binding is None or binding.account == 'unbound' or binding.origin == 'local':
        return ''
    uuid.UUID(binding.account); uuid.UUID(binding.device)
    from device_identity import server_origin
    return json.dumps([server_origin(binding.origin), binding.account, binding.device], separators=(',', ':'))


class DiagnosticStore:
    def __init__(self, root, clock=time.time):
        self.root = Path(root) / 'diagnostics'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'outbox.sqlite3'
        self.clock = clock
        with self.db() as db:
            db.execute('PRAGMA auto_vacuum=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0, receipt TEXT, error TEXT NOT NULL DEFAULT \'\')')
            db.execute('CREATE INDEX IF NOT EXISTS diagnostics_owner_due ON reports(owner,state,due)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=.5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('PRAGMA max_page_count=1024')
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db
        finally:
            db.close()

    def _clean(self, db):
        db.execute('DELETE FROM reports WHERE expires <= ?', (self.clock(),))
        db.execute("UPDATE reports SET state='pending',lease=0 WHERE state='sending' AND lease<?", (self.clock(),))

    def enqueue(self, owner, kind, version, log, *, fingerprint='', occurred_at=None, build_commit=None, os_version=None):
        if kind not in SUMMARIES:
            raise ValueError('diagnostic_kind')
        log = sanitize_log(log)
        if not log:
            raise ValueError('diagnostic_empty')
        ident = str(uuid.uuid4())
        now = self.clock()
        payload = dict(requestId=ident, kind=kind, appVersion=version, occurredAt=occurred_at or utc_now(now),
            summary=SUMMARIES[kind], log=log, buildCommit=build_commit, osVersion=os_version)
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if len(log.encode()) > MAX_LOG_BYTES:
            raise ValueError('diagnostic_limit')
        fingerprint = hashlib.sha256((version + ':' + fingerprint).encode()).hexdigest() if fingerprint else ''
        with self.db() as db:
            self._clean(db)
            if kind != 'manual' and fingerprint:
                old = db.execute('SELECT id FROM reports WHERE owner=? AND fingerprint=? AND created>?', (owner, fingerprint, now-86400)).fetchone()
                if old:
                    return old['id']
            # Prefer removing completed receipts. Never evict another in-flight report.
            db.execute("DELETE FROM reports WHERE id IN (SELECT id FROM reports WHERE state='received' ORDER BY created LIMIT MAX(0,(SELECT COUNT(*) FROM reports)-19))")
            count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM reports').fetchone()
            if count >= MAX_ITEMS or size + len(text.encode()) > MAX_BYTES:
                raise ValueError('diagnostic_queue_full')
            db.execute("INSERT INTO reports(id,owner,kind,created,expires,payload,fingerprint,state) VALUES (?,?,?,?,?,?,?,'pending')",
                       (ident, owner, kind, now, now+TTL, text, fingerprint))
        return ident

    def rows(self, owner, include_unbound=False):
        with self.db() as db:
            self._clean(db)
            return [dict(row) for row in db.execute('SELECT * FROM reports WHERE owner=? OR (? AND owner=\'\') ORDER BY created DESC', (owner, include_unbound))]

    def claim(self, owner, ident=None):
        if not owner:
            return None
        with self.db() as db:
            self._clean(db)
            query = "SELECT * FROM reports WHERE owner=? AND state='pending' AND due<=?"
            params = [owner, self.clock()]
            if ident:
                query += ' AND id=?'; params.append(ident)
            row = db.execute(query+' ORDER BY created LIMIT 1', params).fetchone()
            if row is None:
                return None
            db.execute("UPDATE reports SET state='sending',lease=?,attempts=attempts+1 WHERE id=?", (self.clock()+60, row['id']))
            result = dict(row); result['attempts'] += 1
            return result

    def finish(self, row, *, receipt=None, status=0, retry_after=0):
        with self.db() as db:
            if receipt:
                db.execute("UPDATE reports SET state='received',receipt=?,lease=0,error='' WHERE id=? AND owner=?", (json.dumps(receipt), row['id'], row['owner']))
            else:
                terminal = status in (400, 409, 413, 422) or row['attempts'] >= 30
                # Honor Retry-After even above the normal six-hour backoff cap.
                delay = max(retry_after, min(21600, 30 * 2 ** min(row['attempts'], 10)))
                if status in (401, 403): delay = max(delay, 300)
                db.execute('UPDATE reports SET state=?,due=?,lease=0,error=? WHERE id=? AND owner=?',
                    ('rejected' if terminal else 'pending', self.clock()+delay, 'http_'+str(status) if status else 'network_unavailable', row['id'], row['owner']))

    def bind_unbound(self, ident, owner):
        """Only called after explicit preview confirmation; never automatic."""
        if not owner:
            raise ValueError('diagnostic_login_required')
        with self.db() as db:
            row = db.execute("SELECT payload FROM reports WHERE id=? AND owner='' AND state='pending'", (ident,)).fetchone()
            if row is None:
                raise ValueError('diagnostic_unbound_missing')
            payload = json.loads(row['payload'])
            payload['kind'] = 'manual'; payload['summary'] = SUMMARIES['manual']
            db.execute("UPDATE reports SET owner=?,kind='manual',payload=?,due=0 WHERE id=?", (owner, json.dumps(payload, ensure_ascii=False), ident))

    def retry_manual(self, ident, owner):
        with self.db() as db:
            # Preserve quota Retry-After. This signals intent, not a rate-limit bypass.
            row = db.execute("SELECT id FROM reports WHERE id=? AND owner=? AND state='pending'", (ident, owner)).fetchone()
            return bool(row)
