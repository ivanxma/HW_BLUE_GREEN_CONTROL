#!/usr/bin/env bash
platform_install_python() {
  [[ "${SKIP_PRIVILEGED_SETUP:-0}" != 1 ]] || { echo 'Install Python 3.12 and venv support manually' >&2; return 1; }
  brew install python@3.12 >&2
}
platform_open_firewall() {
  echo "For local use, keep the listener on 127.0.0.1."
}

platform_install_mysql_dependencies() { :; }
platform_prepare_mysql_compat() { :; }
platform_prepare_mysql_security() { :; }
