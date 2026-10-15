#!/usr/bin/env bash
platform_install_python() {
  [[ "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]] || { echo 'Install Python 3.12 and venv support manually' >&2; return 1; }
  sudo dnf install -y python3.12 python3.12-pip python3.12-devel >&2
}
platform_open_firewall() {
  if command -v firewall-cmd >/dev/null; then
    sudo timeout 20 systemctl enable --now firewalld || true
    local zone
    zone="$(sudo timeout 10 firewall-cmd --get-default-zone 2>/dev/null || true)"; zone="${zone:-public}"
    sudo timeout 10 firewall-cmd --zone="$zone" --permanent --add-port="$1/tcp" || true
    sudo timeout 10 firewall-cmd --reload || true
  else echo "Allow TCP port $1 in the host firewall and OCI NSG." >&2; fi
}

platform_install_mysql_dependencies() {
  if [[ "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]]; then
    sudo dnf install -y xz libaio ncurses-compat-libs libstdc++ curl openssl >&2
  fi
}
platform_prepare_mysql_compat() { :; }
platform_prepare_mysql_security() { :; }
