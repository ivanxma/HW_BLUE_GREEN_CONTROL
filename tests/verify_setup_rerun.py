"""Verify rerun preservation only on a fresh local bootstrap account; never reset an existing account."""
import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mysql.connector
from modules.local_auth import LocalAuth, BOOTSTRAP_PASSWORD
from modules.store import Store

root=Path(__file__).resolve().parents[1]
store=Store(root/'.data/store.json')
auth=LocalAuth(store.connection_options['unix_socket'],store)
if not store.must_change_password('admin') or not auth.authenticate('admin',BOOTSTRAP_PASSWORD):
    raise SystemExit('This check only runs on the untouched local bootstrap account')
if any(j['status']=='RUNNING' for j in store.history()):
    raise SystemExit('Wait for the active workflow before testing setup')
password=secrets.token_urlsafe(32)
marker='setup-verification:'+uuid.uuid4().hex
recovery=root/'.data/setup-verification-password'
recovery.write_text(password);recovery.chmod(0o600)
changed=False
try:
    auth.change_password('admin',BOOTSTRAP_PASSWORD,password);changed=True
    store.put(marker,{'source_id':'test-db','secret_id':'test-vault-reference','original_event_state':'ENABLED'})
    env={**os.environ,'SKIP_PRIVILEGED_SETUP':'0','BLUEGREEN_DEPENDENCY_AUDIT':'off'}
    args=[str(root/'setup.sh'),'--platform','ol9','--mode','https','--read-only']
    subprocess.run(args,cwd=root,env=env,check=True,
                   stdout=subprocess.DEVNULL)
    assert auth.authenticate('admin',password)
    assert not auth.authenticate('admin',BOOTSTRAP_PASSWORD)
    assert not store.must_change_password('admin')
    assert store.get(marker)['original_event_state']=='ENABLED'
    print('Setup rerun passed: changed admin password, first-login state and registrations preserved.')
finally:
    auth.connections.close_all()
    subprocess.run([sys.executable,str(root/'embedded_runtime.py'),'start'],check=True)
    if changed:
        connection=mysql.connector.connect(unix_socket=store.connection_options['unix_socket'],user='admin',password=password)
        cursor=connection.cursor();cursor.execute("ALTER USER 'admin'@'localhost' IDENTIFIED BY %s",(BOOTSTRAP_PASSWORD,))
        cursor.close();connection.close()
    with store.db() as cursor:
        cursor.execute("UPDATE operators SET must_change_password=TRUE WHERE username='admin'")
        cursor.execute('DELETE FROM settings WHERE `key`=%s',(marker,))
    recovery.unlink(missing_ok=True)
