#!/usr/bin/env python3
"""Provision and control the application-owned socket-only MySQL server."""
import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import mysql.connector
from mysql.connector.constants import ClientFlag

from modules.embedded import ROOT, install, binary_environment
from modules.local_auth import BOOTSTRAP_PASSWORD
from modules.store import Store

DATA = ROOT / '.data'
CONFIG = ROOT / 'etc/my.cnf'
SOCKET = DATA / 'run/mysql.sock'
CREDENTIALS = DATA / 'store.json'


def write_config():
    for directory in [DATA, DATA/'mysql', DATA/'run', DATA/'log', DATA/'tmp', CONFIG.parent]:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # MySQL option files accept quoted paths. Reject characters that could inject options.
    if any(c in str(ROOT) for c in ['\n','\r','"','\\']):
        raise ValueError('Application directory contains unsupported MySQL option-file characters')
    options = {'basedir':ROOT/'.embedded/mysql-server/current','datadir':DATA/'mysql',
               'socket':SOCKET,'pid-file':DATA/'run/mysqld.pid','log-error':DATA/'log/mysqld.err','tmpdir':DATA/'tmp'}
    CONFIG.write_text('[mysqld]\n' + ''.join(f'{k}="{v}"\n' for k,v in options.items()) +
                      'skip-networking\nmysqlx=0\nbind-address=127.0.0.1\n'
                      'general-log=OFF\nslow-query-log=OFF\nlog-raw=OFF\n')
    CONFIG.chmod(0o600)


def socket_available():
    if not SOCKET.exists():
        return False
    try:
        c = mysql.connector.connect(unix_socket=str(SOCKET),user='__health_probe__',password='',connection_timeout=2)
        c.close()
        return True
    except mysql.connector.Error as exc:
        return exc.errno == 1045


def start():
    if socket_available():
        return
    if not CONFIG.exists():
        raise ValueError('Run setup.sh to provision embedded MySQL first')
    binary = ROOT/'.embedded/mysql-server/current/bin/mysqld'
    process = subprocess.Popen([str(binary), '--defaults-file='+str(CONFIG)], env=binary_environment(),
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError('Embedded mysqld exited during startup; inspect the private .data/log/mysqld.err file')
        if socket_available():
            return
        time.sleep(.5)
    raise RuntimeError('Embedded MySQL did not start; inspect the private .data/log/mysqld.err file')


def stop():
    if not socket_available():
        return
    if not CREDENTIALS.exists():
        raise ValueError('Service credentials are unavailable; stop the application-owned mysqld process manually')
    pid_file = DATA / 'run/mysqld.pid'
    pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
    connection = mysql.connector.connect(**json.loads(CREDENTIALS.read_text()),connection_timeout=5)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute('SHUTDOWN')
        except mysql.connector.Error as exc:
            if exc.errno not in {2006,2013}:
                raise
    finally:
        connection.close()
    for _ in range(240):
        # The socket disappears before InnoDB releases its datadir lock. Wait for process exit.
        if pid is not None:
            try:
                os.kill(pid, 0)
                state = subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
                exited = not state or state.startswith('Z')
            except ProcessLookupError:
                exited = True
        else:
            exited = not pid_file.exists()
        if exited and not socket_available():
            return
        time.sleep(.5)
    raise RuntimeError('Embedded MySQL shutdown timed out')


def provision(platform):
    # This command runs only from deployment setup, never from a web request.
    write_config()
    existing = (DATA/'mysql/auto.cnf').exists()
    if CREDENTIALS.exists():
        start()
        if any(j['status'] == 'RUNNING' for j in Store(CREDENTIALS).history()):
            raise ValueError('Wait for the active blue-green workflow to finish before upgrading embedded MySQL')
        stop()
    server = install('server',platform)
    if not existing:
        subprocess.run([str(ROOT/'.embedded/mysql-server/current/bin/mysqld'),
                        '--defaults-file='+str(CONFIG),'--initialize'],env=binary_environment(),check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    start()
    if not CREDENTIALS.exists():
        # Resume a partially completed bootstrap without resetting a changed admin password.
        try:
            admin = mysql.connector.connect(unix_socket=str(SOCKET),user='admin',password=BOOTSTRAP_PASSWORD)
        except mysql.connector.Error:
            content = (DATA/'log/mysqld.err').read_text()
            passwords = re.findall(r'temporary password.*root@localhost: (\S+)',content)
            if not passwords:
                raise RuntimeError('Bootstrap cannot continue; admin password has changed or initialization was incomplete')
            root = mysql.connector.connect(unix_socket=str(SOCKET), user='root', password=passwords[-1],
                                           client_flags=[ClientFlag.CAN_HANDLE_EXPIRED_PASSWORDS])
            cursor = root.cursor()
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s",(BOOTSTRAP_PASSWORD,))
            cursor.execute("RENAME USER 'root'@'localhost' TO 'admin'@'localhost'")
            cursor.close();root.close()
            admin = mysql.connector.connect(unix_socket=str(SOCKET),user='admin',password=BOOTSTRAP_PASSWORD)
        cursor = admin.cursor()
        import secrets
        service_password = secrets.token_urlsafe(48)
        cursor.execute('CREATE DATABASE IF NOT EXISTS bluegreen CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        cursor.execute("CREATE USER IF NOT EXISTS 'bluegreen_store'@'localhost' IDENTIFIED BY %s",(service_password,))
        cursor.execute("ALTER USER 'bluegreen_store'@'localhost' IDENTIFIED BY %s",(service_password,))
        cursor.execute("GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX ON bluegreen.* TO 'bluegreen_store'@'localhost'")
        cursor.execute("GRANT SHUTDOWN ON *.* TO 'bluegreen_store'@'localhost'")
        cursor.close();admin.close()
        options = {'unix_socket':str(SOCKET),'user':'bluegreen_store','password':service_password,'database':'bluegreen'}
        fd = os.open(CREDENTIALS,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as handle:
            json.dump(options,handle)
    store = Store(CREDENTIALS)
    with store.db() as cursor:
        # Idempotent: a setup rerun cannot re-enable the first-login flag.
        cursor.execute("INSERT IGNORE INTO operators(username,must_change_password) VALUES('admin',TRUE)")
    store.migrate_sqlite(DATA/'operations.sqlite3')
    store.put('runtime:server',server)
    print('Embedded registration database provisioned; admin password changes are preserved on reruns.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['provision','start','stop','wait','install-shell','discover'])
    parser.add_argument('--platform',choices=['ol9','macos'],default='macos' if os.uname().sysname=='Darwin' else 'ol9')
    args=parser.parse_args()
    if args.action=='provision':provision(args.platform)
    elif args.action=='start':start()
    elif args.action=='stop':stop()
    elif args.action=='wait':
        for _ in range(120):
            if socket_available():break
            time.sleep(.5)
        else:raise RuntimeError('Embedded MySQL readiness check timed out')
    elif args.action=='install-shell':
        package=install('shell',args.platform)
        if CREDENTIALS.exists():Store(CREDENTIALS).put('runtime:mysqlsh',package)
        print('Embedded MySQL Shell '+package['version']+' installed.')
    else:
        from modules.embedded import discover
        print(json.dumps({c:discover(c,args.platform) for c in ['server','shell']},indent=2))


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Do not expose SQL exception text, which may contain bootstrap credentials.
        if isinstance(exc,(ValueError,RuntimeError)):
            raise SystemExit(str(exc))
        raise SystemExit(type(exc).__name__+': embedded MySQL setup failed; inspect local deployment state')
