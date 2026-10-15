#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
platform=""; mode="none"; setup_host=""; setup_port=""; service_user="$(id -un)"; setup_read_only="${BLUEGREEN_OCI_READ_ONLY:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) platform="$2"; shift 2;;
    --mode) mode="$2"; shift 2;;
    --host) setup_host="$2"; shift 2;;
    --port) setup_port="$2"; shift 2;;
    --service-user) service_user="$2"; shift 2;;
    --read-only) setup_read_only=1; shift;;
    --execute-oci) setup_read_only=0; shift;;
    *) echo "Usage: setup.sh [--platform ol9] [--mode none|http|https] [--host HOST] [--port PORT] [--service-user USER] [--read-only|--execute-oci]" >&2; exit 2;;
  esac
done
if [[ -z "$platform" ]]; then
  if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    case "$ID" in ol) platform="ol${VERSION_ID%%.*}";; *) echo 'Specify a supported platform' >&2; exit 2;; esac
  fi
fi
if [[ -z "$setup_host" ]]; then [[ "$mode" == none ]] && setup_host=127.0.0.1 || setup_host=0.0.0.0; fi
if [[ -z "$setup_port" ]]; then
  case "$mode" in https) setup_port=443;; http) setup_port=8080;; *) setup_port=5000;; esac
fi
[[ "$platform" == ol9 && -f /etc/os-release ]] || { echo 'Only Oracle Linux 9 is supported' >&2; exit 2; }
source /etc/os-release
[[ "$ID" == ol && "${VERSION_ID%%.*}" == 9 ]] || { echo 'Only Oracle Linux 9 is supported' >&2; exit 2; }
[[ "$mode" =~ ^(none|http|https)$ ]] || { echo 'Unsupported mode' >&2; exit 2; }
[[ "$setup_port" =~ ^[0-9]+$ ]] && ((setup_port > 0 && setup_port < 65536)) || { echo 'Invalid port' >&2; exit 2; }
[[ "$service_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || { echo 'Invalid service user' >&2; exit 2; }
[[ "$service_user" == "$(id -un)" && "$service_user" != root ]] || { echo 'Run setup as the runtime user (opc on Oracle Linux 9 Compute), using sudo only for platform prerequisites.' >&2; exit 2; }
source "$platform/setup_platform.sh"
python_bin="${BLUEGREEN_PYTHON_BIN:-$(command -v python3.12 || command -v python3 || true)}"
minimum="${BLUEGREEN_PYTHON_MIN_VERSION:-3.12}"
if [[ -z "$python_bin" ]] || ! "$python_bin" -c "import sys;sys.exit(sys.version_info[:2] < tuple(map(int,'$minimum'.split('.'))))"; then
  platform_install_python
  python_bin="$(command -v python3.12 || true)"
fi
[[ -n "$python_bin" && "$python_bin" != *$'\n'* && -x "$python_bin" ]] || { echo 'Set BLUEGREEN_PYTHON_BIN to Python 3.12 or newer' >&2; exit 1; }
"$python_bin" -c "import sys;sys.exit(sys.version_info[:2] < tuple(map(int,'$minimum'.split('.'))))"
"$python_bin" -m venv .venv || { platform_install_python; "$python_bin" -m venv .venv; }
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python - "$mode" "$setup_host" "$setup_port" "$python_bin" "$minimum" "$setup_read_only" <<'PY'
import shlex,sys
from pathlib import Path
mode,host,port,python,minimum,read_only=sys.argv[1:]
p=Path('.runtime.env'); lines=p.read_text().splitlines() if p.exists() else []
settings={'HOST':host,'PORT':port,'BLUEGREEN_DEPLOY_MODE':mode,'BLUEGREEN_PYTHON_BIN':python,
          'BLUEGREEN_PYTHON_MIN_VERSION':minimum,'BLUEGREEN_DEMO':'0','BLUEGREEN_COOKIE_SECURE':'1' if mode=='https' else '0'}
if read_only: settings['BLUEGREEN_OCI_READ_ONLY']=read_only
lines=[x for x in lines if x.partition('=')[0] not in settings]
lines += [k+'='+shlex.quote(v) for k,v in settings.items()]
p.write_text('\n'.join(lines)+'\n'); p.chmod(0o600)
PY
mkdir -p .data; chmod 700 .data
platform_install_mysql_dependencies
platform_prepare_mysql_compat
platform_prepare_mysql_security
.venv/bin/python embedded_runtime.py provision --platform "$platform"
"$platform/install_mysql_shell_innovation.sh"
.venv/bin/python - <<'PY2'
from pathlib import Path
from modules.store import Store
import shlex
store=Store('.data/store.json')
settings={'BLUEGREEN_STORE_CONFIG':str(Path('.data/store.json').resolve()),
          'BLUEGREEN_MYSQL_SERVER_VERSION':store.get('runtime:server')['version'],
          'BLUEGREEN_MYSQLSH_VERSION':store.get('runtime:mysqlsh')['version'],
          'BLUEGREEN_MYSQLSH':str(Path('.embedded/mysql-shell/current/bin/mysqlsh').resolve())}
p=Path('.runtime.env');lines=p.read_text().splitlines()
lines=[line for line in lines if line.partition('=')[0] not in settings and not line.startswith(('BLUEGREEN_ADMIN_USER=','BLUEGREEN_ADMIN_PASSWORD_HASH='))]
lines.extend(k+'='+shlex.quote(v) for k,v in settings.items())
p.write_text('\n'.join(lines)+'\n');p.chmod(0o600)
PY2

if [[ "$mode" == https && (! -f tls/server.crt || ! -f tls/server.key) ]]; then
  mkdir -p tls; chmod 700 tls
  openssl req -x509 -newkey rsa:3072 -nodes -keyout tls/server.key -out tls/server.crt -days 365 -subj '/CN=heatwave-bluegreen'
  chmod 600 tls/server.key
  echo 'Replace the generated certificate with a trusted certificate for external access.'
fi
if [[ "$mode" != none && "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]]; then
 .venv/bin/python - "$PWD" "$mode" "$service_user" <<'PY'
import subprocess,sys
from pathlib import Path
root,mode,user=sys.argv[1:]
service=f'heatwave-bluegreen-{mode}.service'
text=f'''[Unit]
Description=HeatWave Blue Green console ({mode})
After=network-online.target heatwave-bluegreen-local-mysql.service
Requires=heatwave-bluegreen-local-mysql.service
Wants=network-online.target
[Service]
Type=simple
User={user}
SELinuxContext=system_u:system_r:unconfined_service_t:s0
WorkingDirectory={root}
ExecStart=/usr/bin/bash "{root}/start_{mode}.sh"
Restart=on-failure
RestartSec=5
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
[Install]
WantedBy=multi-user.target
'''
# Replace the setup-launched process with the supervised socket-only server.
subprocess.run([str(Path('.venv/bin/python').absolute()), 'embedded_runtime.py', 'stop'],check=True)
mysql_unit=f"""[Unit]
Description=HeatWave Blue Green embedded MySQL
After=network.target
[Service]
Type=simple
User={user}
SELinuxContext=system_u:system_r:unconfined_service_t:s0
WorkingDirectory={root}
ExecStart=/usr/bin/bash "{root}/start_mysql.sh"
ExecStartPost=/usr/bin/bash "{root}/wait_mysql.sh"
TimeoutStartSec=120
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
UMask=0077
[Install]
WantedBy=multi-user.target
"""
mysql_service='heatwave-bluegreen-local-mysql.service'
mysql_local=Path('.data')/mysql_service;mysql_local.write_text(mysql_unit)
subprocess.run(['sudo','install','-m','644',str(mysql_local),'/etc/systemd/system/'+mysql_service],check=True)
local=Path('.data')/service;local.write_text(text)
subprocess.run(['sudo','install','-m','644',str(local),'/etc/systemd/system/'+service],check=True)
subprocess.run(['sudo','systemctl','daemon-reload'],check=True)
subprocess.run(['sudo','systemctl','enable','--now',mysql_service],check=True)
other='https' if mode=='http' else 'http'
subprocess.run(['sudo','systemctl','disable','--now',f'heatwave-bluegreen-{other}.service'],check=False)
subprocess.run(['sudo','systemctl','enable',service],check=True)
# An already-running web process keeps its old environment and imported code.
# Apply saved mode/configuration changes on every successful setup rerun.
subprocess.run(['sudo','systemctl','restart',service],check=True)
PY
 platform_open_firewall "$setup_port"
fi
if [[ "$mode" == none && "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]]; then
  sudo systemctl disable --now heatwave-bluegreen-http.service heatwave-bluegreen-https.service 2>/dev/null || true
fi
if [[ "${BLUEGREEN_DEPENDENCY_AUDIT:-on}" != off ]]; then
 .venv/bin/python -m pip install pip-audit >/dev/null
 .venv/bin/python -m pip_audit --cache-dir .data/pip-audit-cache || echo 'Dependency audit reported issues; inspect the output above.' >&2
fi
echo 'Setup complete. Fresh installations use admin with the initial password documented in README; first login requires changing it.'
