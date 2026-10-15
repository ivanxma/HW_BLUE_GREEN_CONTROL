#!/usr/bin/env python3
"""Collect deployment evidence without password files, session values or private keys."""
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

from modules.store import Store

root=Path(__file__).resolve().parent
result={'platform':platform.platform(),'architecture':platform.machine(),'python':platform.python_version(),'commands':{}}
commands={
 'OS':['cat','/etc/os-release'],
 'SELinux':['getenforce'],
 'Web service':['sudo','systemctl','status','heatwave-bluegreen-https.service','--no-pager','-n','8'],
 'MySQL service':['sudo','systemctl','status','heatwave-bluegreen-local-mysql.service','--no-pager','-n','8'],
 'Unit syntax':['sudo','systemd-analyze','verify','/etc/systemd/system/heatwave-bluegreen-https.service','/etc/systemd/system/heatwave-bluegreen-local-mysql.service'],
 'Listeners':['sudo','ss','-ltnp'],
 'Firewalld active zones':['sudo','firewall-cmd','--get-active-zones'],
 'Firewalld ports':['sudo','firewall-cmd','--list-ports'],
 'Firewalld services':['sudo','firewall-cmd','--list-services'],
 'Firewalld permanent 443':['sudo','firewall-cmd','--permanent','--query-port=443/tcp'],
 'Local HTTPS':['curl','-k','-s','-o','/dev/null','-w','%{http_code}', 'https://127.0.0.1/login'],
 'MySQL binary':['.embedded/mysql-server/current/bin/mysqld','--version'],
 'MySQL Shell':['./mysqlsh.sh','--version'],
 'Certificate':['openssl','x509','-in','tls/server.crt','-noout','-subject','-issuer','-dates','-fingerprint','-sha256'],
 'Process contexts':['ps','-e','-o','label,pid,user,comm'],
 'Web unit':['sudo','systemctl','cat','heatwave-bluegreen-https.service'],
 'MySQL unit':['sudo','systemctl','cat','heatwave-bluegreen-local-mysql.service'],
}
for name,command in commands.items():
 response=subprocess.run(command,cwd=root,capture_output=True,text=True)
 text=response.stdout+response.stderr
 if name=='Process contexts':text='\n'.join(line for line in text.splitlines() if 'mysqld' in line or 'gunicorn' in line)
 result['commands'][name]={'exit_code':response.returncode,'output':text.strip()}
store=Store(root/'.data/store.json')
with store.db() as cursor:
 cursor.execute('SELECT VERSION() AS version, @@skip_networking AS socket_only, CURRENT_USER() AS account')
 result['database']=cursor.fetchone()
 cursor.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
 result['database']['uptime']=cursor.fetchone()['Value']
 result['database']['table_counts']={}
 for table in ['settings','jobs','events','operators']:
  cursor.execute('SELECT COUNT(*) AS count FROM '+table)
  result['database']['table_counts'][table]=cursor.fetchone()['count']
result['database']['admin_first_login_required']=store.must_change_password('admin')
result['installed_releases']={key:store.get('runtime:'+key) for key in ['server','mysqlsh']}
result['file_modes']={str(p.relative_to(root)):oct(p.stat().st_mode & 0o777) for p in [root/'.data',root/'.data/store.json',root/'.runtime.env',root/'tls/server.key']}
excluded={'.data','.embedded','.venv','downloads','tls','validation','__pycache__','.pytest_cache'}
result['source_hashes']={}
for file in sorted(root.rglob('*')):
 if file.is_file() and not any(x in excluded for x in file.relative_to(root).parts) and file.name!='.runtime.env' and file.suffix in {'.py','.sh','.js','.html','.css','.md','.toml','.txt'}:
  result['source_hashes'][str(file.relative_to(root))]=hashlib.sha256(file.read_bytes()).hexdigest()
setup_log=Path('/home/opc/bluegreen-setup-validation.log').read_text()
result['setup_log_tail']='\n'.join(line for line in setup_log.splitlines()[-35:] if not any(s in line.lower() for s in ['password','private key','authorization:','set-cookie']))
Path(sys.argv[1]).write_text(json.dumps(result,indent=2))
print('Deployment evidence collected without credential values.')
