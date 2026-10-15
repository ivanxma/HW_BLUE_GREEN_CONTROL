"""Embedded MySQL registration database and durable workflow journal."""
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import mysql.connector

DDL = [
    '''CREATE TABLE IF NOT EXISTS jobs (
       id VARCHAR(36) PRIMARY KEY, resource VARCHAR(512) NOT NULL, kind VARCHAR(64),
       status VARCHAR(32), started DOUBLE, ended DOUBLE, params LONGTEXT,
       active_resource VARCHAR(512) GENERATED ALWAYS AS
         (CASE WHEN status='RUNNING' THEN resource ELSE NULL END) STORED,
       UNIQUE KEY one_active_job(active_resource)) ENGINE=InnoDB''',
    '''CREATE TABLE IF NOT EXISTS events (
       id BIGINT AUTO_INCREMENT PRIMARY KEY, job VARCHAR(36), step VARCHAR(256),
       status VARCHAR(32), started DOUBLE, ended DOUBLE, detail LONGTEXT,
       work_request VARCHAR(512), KEY job_events(job)) ENGINE=InnoDB''',
    '''CREATE TABLE IF NOT EXISTS settings (
       `key` VARCHAR(512) PRIMARY KEY, value LONGTEXT) ENGINE=InnoDB''',
    '''CREATE TABLE IF NOT EXISTS operators (
       username VARCHAR(32) PRIMARY KEY, must_change_password BOOLEAN NOT NULL DEFAULT TRUE)
       ENGINE=InnoDB''',
]


class Store:
    def __init__(self, connection_file):
        path = Path(connection_file)
        if not path.exists():
            raise RuntimeError('Embedded MySQL is not configured. Run setup.sh before starting the application.')
        path.chmod(0o600)
        self.connection_options = json.loads(path.read_text())
        if not self.connection_options.get('unix_socket') or any(k in self.connection_options for k in ('host', 'port')):
            raise ValueError('Registration database must use the embedded MySQL Unix socket')
        with self.db() as cursor:
            for statement in DDL:
                cursor.execute(statement)

    @contextmanager
    def db(self):
        connection = mysql.connector.connect(**self.connection_options, connection_timeout=5, autocommit=False)
        cursor = connection.cursor(dictionary=True, buffered=True)
        try:
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='INTERRUPTED',ended=%s WHERE status='RUNNING'", (time.time(),))
            db.execute("UPDATE events SET status='INTERRUPTED',ended=%s,detail='Worker restarted; reconcile OCI state before another operation' WHERE status='RUNNING'", (time.time(),))

    def put(self, key, value):
        with self.db() as db:
            db.execute('INSERT INTO settings (`key`,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=%s',
                       (key, json.dumps(value, default=str), json.dumps(value, default=str)))

    def get(self, key, default=None):
        with self.db() as db:
            db.execute('SELECT value FROM settings WHERE `key`=%s', (key,))
            row = db.fetchone()
        return json.loads(row['value']) if row else default

    def start(self, resource, kind, params):
        jid = str(uuid.uuid4())
        with self.db() as db:
            try:
                db.execute('INSERT INTO jobs(id,resource,kind,status,started,ended,params) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                           (jid, resource, kind, 'RUNNING', time.time(), None, json.dumps(params, default=str)))
            except mysql.connector.IntegrityError as exc:
                raise ValueError('An operation is already running for this resource') from exc
        return jid

    def finish(self, jid, status):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=%s,ended=%s WHERE id=%s', (status, time.time(), jid))

    def event(self, jid, step):
        with self.db() as db:
            db.execute('INSERT INTO events(job,step,status,started) VALUES(%s,%s,%s,%s)',
                       (jid, step, 'RUNNING', time.time()))
            return db.lastrowid

    def update_event(self, eid, status=None, detail=None, work_request=None):
        with self.db() as db:
            if status:
                db.execute('UPDATE events SET status=%s,ended=%s WHERE id=%s', (status, time.time(), eid))
            if detail:
                db.execute('UPDATE events SET detail=%s WHERE id=%s', (detail, eid))
            if work_request:
                db.execute('UPDATE events SET work_request=%s WHERE id=%s', (work_request, eid))

    def history(self):
        with self.db() as db:
            db.execute('SELECT id,resource,kind,status,started,ended,params FROM jobs ORDER BY started DESC LIMIT 100')
            jobs = db.fetchall()
            for job in jobs:
                job['params'] = json.loads(job['params'])
                db.execute('SELECT * FROM events WHERE job=%s ORDER BY id', (job['id'],))
                job['events'] = db.fetchall()
        return jobs

    def must_change_password(self, username):
        with self.db() as db:
            db.execute('SELECT must_change_password FROM operators WHERE username=%s', (username,))
            row = db.fetchone()
        if row is None:
            raise ValueError('Operator has not been registered in the embedded database')
        return bool(row['must_change_password'])

    def password_changed(self, username):
        with self.db() as db:
            db.execute('UPDATE operators SET must_change_password=FALSE WHERE username=%s', (username,))

    def migrate_sqlite(self, source):
        """One-time transactional import; the old file remains a read-only recovery copy."""
        source = Path(source)
        if not source.exists() or self.get('migration:sqlite:v1'):
            return
        import sqlite3
        source.chmod(0o600)
        legacy = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        legacy.row_factory = sqlite3.Row
        try:
            with self.db() as db:
                for table, columns in [('settings', ['key', 'value']),
                                       ('jobs', ['id','resource','kind','status','started','ended','params']),
                                       ('events', ['id','job','step','status','started','ended','detail','work_request'])]:
                    quoted = ','.join('`'+c+'`' for c in columns)
                    placeholders = ','.join(['%s'] * len(columns))
                    for row in legacy.execute(f'SELECT {quoted} FROM {table}'):
                        db.execute(f'INSERT INTO {table}({quoted}) VALUES({placeholders})', tuple(row[c] for c in columns))
                db.execute('INSERT INTO settings (`key`,value) VALUES(%s,%s)',
                           ('migration:sqlite:v1', json.dumps({'completed': time.time()})))
        finally:
            legacy.close()
