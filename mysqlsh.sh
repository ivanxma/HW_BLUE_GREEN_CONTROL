#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export MYSQLSH_USER_CONFIG_HOME="$PWD/.data/mysqlsh"
exec .embedded/mysql-shell/current/bin/mysqlsh "$@"
