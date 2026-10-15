#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="$PWD/.embedded/mysql-server/compat/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec .embedded/mysql-server/current/bin/mysqld --defaults-file="$PWD/etc/my.cnf"
