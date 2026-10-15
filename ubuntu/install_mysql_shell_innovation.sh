#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python embedded_runtime.py install-shell --platform ubuntu
