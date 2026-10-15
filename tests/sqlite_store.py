"""Durable workflow journal. References only: secret payloads never enter this store."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, resource TEXT, kind TEXT,
              status TEXT, started REAL, ended REAL, params TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(resource)
              WHERE status='RUNNING';
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, job TEXT, step TEXT,
              status TEXT, started REAL, ended REAL, detail TEXT, work_request TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            ''')
        Path(path).chmod(0o600)

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=20) as db:
            db.row_factory = sqlite3.Row
            yield db

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='INTERRUPTED',ended=? WHERE status='RUNNING'", (time.time(),))
            db.execute("UPDATE events SET status='INTERRUPTED',ended=?,detail='Worker restarted; reconcile OCI state before another operation' WHERE status='RUNNING'", (time.time(),))

    def put(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value, default=str)))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def start(self, resource, kind, params):
        jid = str(uuid.uuid4())
        with self.db() as db:
            try:
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',
                           (jid, resource, kind, 'RUNNING', time.time(), None, json.dumps(params, default=str)))
            except sqlite3.IntegrityError as exc:
                raise ValueError('An operation is already running for this resource') from exc
        return jid

    def finish(self, jid, status):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?,ended=? WHERE id=?', (status, time.time(), jid))

    def event(self, jid, step):
        with self.db() as db:
            return db.execute('INSERT INTO events(job,step,status,started) VALUES(?,?,?,?)',
                              (jid, step, 'RUNNING', time.time())).lastrowid

    def update_event(self, eid, status=None, detail=None, work_request=None):
        with self.db() as db:
            if status:
                db.execute('UPDATE events SET status=?,ended=? WHERE id=?', (status, time.time(), eid))
            if detail:
                db.execute('UPDATE events SET detail=? WHERE id=?', (detail, eid))
            if work_request:
                db.execute('UPDATE events SET work_request=? WHERE id=?', (work_request, eid))

    def history(self):
        with self.db() as db:
            jobs = [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY started DESC LIMIT 100')]
            for job in jobs:
                job['params'] = json.loads(job['params'])
                job['events'] = [dict(r) for r in db.execute('SELECT * FROM events WHERE job=? ORDER BY id', (job['id'],))]
        return jobs
