#!/usr/bin/env bash
platform_install_python() {
  [[ "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]] || { echo 'Install Python 3.12 and venv support manually' >&2; return 1; }
  sudo apt-get update >&2
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv python3.12-dev >&2
}
platform_open_firewall() {
  if command -v ufw >/dev/null; then sudo timeout 15 ufw allow "$1/tcp" || true
  else echo "Allow TCP port $1 in the host firewall and OCI NSG." >&2; fi
}

platform_install_mysql_dependencies() {
  if [[ "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]]; then
    sudo apt-get update >&2
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y xz-utils libaio1t64 libncurses6 libstdc++6 curl openssl >&2 ||
      sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y xz-utils libaio1 libncurses6 libstdc++6 curl openssl >&2
  fi
}
platform_prepare_mysql_compat() {
  local library
  library="$(ldconfig -p | awk '/libaio.so.1t64/ {print $NF; exit}')"
  if [[ -n "$library" ]]; then
    mkdir -p .embedded/mysql-server/compat/lib
    ln -sfn "$library" .embedded/mysql-server/compat/lib/libaio.so.1
  fi
}
platform_prepare_mysql_security() {
  if [[ -f /etc/apparmor.d/usr.sbin.mysqld && "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]]; then
    mkdir -p .data
    local policy=.data/apparmor-mysqld
    {
      printf '"%s/etc/my.cnf" r,\n' "$PWD"
      printf '"%s/.embedded/mysql-server/**" mrk,\n' "$PWD"
      printf '"%s/.data/" rw,\n' "$PWD"
      printf '"%s/.data/**" rwk,\n' "$PWD"
    } > "$policy"
    sudo touch /etc/apparmor.d/local/usr.sbin.mysqld
    if ! sudo grep -Fq -- "$PWD/.data/**" /etc/apparmor.d/local/usr.sbin.mysqld; then
      sudo tee -a /etc/apparmor.d/local/usr.sbin.mysqld < "$policy" >/dev/null
    fi
    sudo apparmor_parser -r /etc/apparmor.d/usr.sbin.mysqld
  fi
}
