"""Opt-in tests against this application's real embedded, socket-only MySQL."""
import json
import os
import secrets
import uuid
from pathlib import Path

import mysql.connector
import pytest

from app import ROOT, create_app
from modules.demo import DemoService, DemoObserver
from modules.local_auth import LocalAuth, BOOTSTRAP_PASSWORD
from modules.store import Store

STORE_CONFIG=Path(os.environ.get('BLUEGREEN_TEST_STORE_CONFIG',str(ROOT/'.data/store.json')))

pytestmark=pytest.mark.skipif(os.environ.get('BLUEGREEN_MYSQL_INTEGRATION')!='1',reason='Requires the provisioned embedded MySQL server')


def test_real_registration_journal_and_sqlite_migration(tmp_path):
    store=Store(STORE_CONFIG)
    resource='integration-'+str(uuid.uuid4())
    jobs=[]
    try:
        store.put(resource,{'username':'observer','secret_id':'vault-reference','events':[{'status':'ENABLED'}]})
        assert Store(STORE_CONFIG).get(resource)['secret_id']=='vault-reference'
        job=store.start(resource,'create',{'secret_id':'reference'});jobs.append(job)
        eid=store.event(job,'Provision')
        store.update_event(eid,work_request='work-reference')
        with pytest.raises(ValueError):store.start(resource,'create',{})
        store.update_event(eid,status='SUCCEEDED',detail='Completed')
        store.finish(job,'SUCCEEDED')
        history=next(j for j in store.history() if j['id']==job)
        assert history['events'][0]['work_request']=='work-reference'
        assert history['status']=='SUCCEEDED'
        # Confirm socket-only configuration rather than relying only on option-file text.
        with store.db() as db:
            db.execute('SELECT @@skip_networking AS socket_only')
            assert db.fetchone()['socket_only']==1
    finally:
        with store.db() as db:
            for jid in jobs:
                db.execute('DELETE FROM events WHERE job=%s',(jid,))
                db.execute('DELETE FROM jobs WHERE id=%s',(jid,))
            db.execute('DELETE FROM settings WHERE `key`=%s',(resource,))


def test_real_admin_first_login_password_change(tmp_path):
    store=Store(STORE_CONFIG)
    auth=LocalAuth(store.connection_options['unix_socket'],store)
    if not store.must_change_password('admin') or not auth.authenticate('admin',BOOTSTRAP_PASSWORD):
        pytest.skip('Only runs on a fresh default admin account; never resets an existing changed password')
    if any(j['status']=='RUNNING' for j in store.history()):
        pytest.skip('Do not run integration auth tests while a workflow is active')
    service=DemoService();observer=DemoObserver(service)
    app=create_app({'TESTING':True,'DATA_DIR':str(tmp_path)},service,observer,store,auth)
    client=app.test_client();new_password=secrets.token_urlsafe(30)
    changed=False
    try:
        client.get('/login')
        with client.session_transaction() as session:csrf=session['login_csrf']
        response=client.post('/login',data={'csrf':csrf,'username':'admin','password':BOOTSTRAP_PASSWORD})
        assert response.location.endswith('/change-password')
        assert client.get('/api/dashboard').status_code==403
        with client.session_transaction() as session:
            assert set(session)=={'sid'}
            csrf=app.extensions['bluegreen']['sessions'][session['sid']]['csrf']
        response=client.post('/change-password',data={'csrf':csrf,'new_password':new_password,'confirm_password':new_password})
        changed=auth.authenticate('admin',new_password)
        assert response.status_code==302 and changed
        assert not auth.authenticate('admin',BOOTSTRAP_PASSWORD)
        assert not Store(STORE_CONFIG).must_change_password('admin')
        assert client.get('/api/dashboard').status_code==401
        client.get('/login')
        with client.session_transaction() as session:csrf=session['login_csrf']
        assert client.post('/login',data={'csrf':csrf,'username':'admin','password':new_password}).location.endswith('/')
        assert client.get('/api/dashboard').status_code==200
        assert new_password.encode() not in client.get('/').data
    finally:
        # Restore only the initially fresh test account, leaving the user-requested first-login flow ready.
        auth.connections.close_all()
        if changed:
            connection=mysql.connector.connect(unix_socket=store.connection_options['unix_socket'],user='admin',password=new_password)
            cursor=connection.cursor()
            cursor.execute("ALTER USER 'admin'@'localhost' IDENTIFIED BY %s",(BOOTSTRAP_PASSWORD,))
            cursor.close();connection.close()
            with store.db() as db:db.execute("UPDATE operators SET must_change_password=TRUE WHERE username='admin'")



def test_sqlite_import_is_atomic_and_idempotent(tmp_path):
    from sqlite_store import Store as LegacyStore
    settings=json.loads((STORE_CONFIG).read_text())
    try:
        admin=mysql.connector.connect(unix_socket=settings['unix_socket'],user='admin',password=BOOTSTRAP_PASSWORD)
    except mysql.connector.Error:
        pytest.skip('Migration isolation test requires the fresh bootstrap account')
    schema='bluegreen_test_'+uuid.uuid4().hex
    cursor=admin.cursor()
    cursor.execute('CREATE DATABASE `'+schema+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
    connection_file=tmp_path/'migration-connection.json'
    connection_file.write_text(json.dumps({'unix_socket':settings['unix_socket'],'user':'admin',
                                         'password':BOOTSTRAP_PASSWORD,'database':schema}))
    connection_file.chmod(0o600)
    try:
        legacy=LegacyStore(tmp_path/'legacy.sqlite3')
        legacy.put('observer:source',{'username':'repl','secret_id':'vault-reference'})
        legacy.put('registered-events:source',[{'event_schema':'app','event_name':'e','status':'ENABLED'}])
        jid=legacy.start('source','create',{'event_choices':['e']})
        eid=legacy.event(jid,'Create')
        legacy.update_event(eid,status='SUCCEEDED',work_request='work-ref')
        legacy.finish(jid,'SUCCEEDED')
        store=Store(connection_file)
        store.migrate_sqlite(tmp_path/'legacy.sqlite3')
        assert store.get('observer:source')['secret_id']=='vault-reference'
        assert store.get('registered-events:source')[0]['status']=='ENABLED'
        assert store.history()[0]['events'][0]['work_request']=='work-ref'
        store.put('observer:source',{'username':'changed','secret_id':'new-ref'})
        store.migrate_sqlite(tmp_path/'legacy.sqlite3')
        assert store.get('observer:source')['username']=='changed'
        assert len(store.history())==1
        # A later conflicting row must roll back earlier imported registrations.
        with store.db() as db:db.execute('DELETE FROM settings WHERE `key`=%s',('migration:sqlite:v1',))
        conflicting=LegacyStore(tmp_path/'conflicting.sqlite3')
        conflicting.put('atomic-first',{'value':'must roll back'})
        conflicting.put('observer:source',{'username':'conflicting'})
        with pytest.raises(mysql.connector.IntegrityError):store.migrate_sqlite(tmp_path/'conflicting.sqlite3')
        assert store.get('atomic-first') is None
        assert store.get('observer:source')['username']=='changed'
        assert store.get('migration:sqlite:v1') is None
    finally:
        cursor.execute('DROP DATABASE `'+schema+'`')
        cursor.close();admin.close()
