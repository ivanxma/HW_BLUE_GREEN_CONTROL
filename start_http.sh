#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
launch_port="${PORT:-}"
if [[ -f .runtime.env ]]; then set -a; source .runtime.env; set +a; fi
export PORT="${launch_port:-${PORT:-5000}}"
.venv/bin/python embedded_runtime.py start
export MYSQLSH_USER_CONFIG_HOME="$PWD/.data/mysqlsh"
export PATH="$PWD/.embedded/mysql-shell/current/bin:$PATH"
exec .venv/bin/gunicorn --workers 1 --threads 8 --timeout 0 --bind "${HOST:-127.0.0.1}:$PORT" 'app:create_app()'
