#!/usr/bin/env bash
# Adapted from the MyApp Compute bootstrap template for this standalone app.
set -euo pipefail
APP_REPO="${APP_REPO:-https://github.com/ivanxma/HW_BLUE_GREEN_CONTROL.git}"
OS_FAMILY="${OS_FAMILY:-ol9}"
[[ "$OS_FAMILY" == ol9 ]] || { echo "Compute deployment requires Oracle Linux 9" >&2; exit 2; }
[[ -f /etc/os-release ]] || { echo "Compute deployment requires Oracle Linux 9" >&2; exit 2; }
source /etc/os-release
[[ "$ID" == ol && "${VERSION_ID%%.*}" == 9 ]] || { echo "Compute deployment requires Oracle Linux 9" >&2; exit 2; }
default_user=opc
APP_USER="${APP_USER:-$default_user}"
APP_DIR="${APP_DIR:-/home/$APP_USER/heatwave-console}"
APP_SUBDIR="${APP_SUBDIR:-.}"
STATE_DIR=/var/lib/heatwave-bluegreen-init
LOG_FILE=/var/log/heatwave-bluegreen-init.log
mkdir -p "$STATE_DIR"; chmod 755 "$STATE_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1
touch "$STATE_DIR/installing"; rm -f "$STATE_DIR/installed" "$STATE_DIR/failed"
finish() { rm -f "$STATE_DIR/installing"; if [[ "$1" == 0 ]]; then touch "$STATE_DIR/installed"; else touch "$STATE_DIR/failed"; fi; }
trap 'finish $?' EXIT
cat > /etc/profile.d/heatwave-bluegreen.sh <<BANNER
case \$- in *i*) ;; *) return;; esac
[[ "\${USER:-}" == "$APP_USER" ]] || return
if [[ -f "$STATE_DIR/installing" ]]; then echo 'Please wait until installation to be completed.'
elif [[ -f "$STATE_DIR/installed" ]]; then echo 'HeatWave Blue Green setup has been completed'; systemctl --no-pager status heatwave-bluegreen-https.service || true
elif [[ -f "$STATE_DIR/failed" ]]; then echo 'Installation failed. Review $LOG_FILE'; fi
BANNER
chmod 755 /etc/profile.d/heatwave-bluegreen.sh
if ! command -v git >/dev/null; then
 dnf install -y git
fi
if [[ -d "$APP_DIR/.git" ]]; then
 sudo -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
 sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
else
 if [[ -e "$APP_DIR" ]]; then mv "$APP_DIR" "$APP_DIR.$(date +%Y%m%d%H%M%S)"; fi
 sudo -u "$APP_USER" git clone "$APP_REPO" "$APP_DIR"
fi
sudo -u "$APP_USER" bash "$APP_DIR/$APP_SUBDIR/setup.sh" --platform "$OS_FAMILY" --mode https --host 0.0.0.0 --port 443 --service-user "$APP_USER"
systemctl --no-pager status heatwave-bluegreen-https.service || true
echo "First web login must change the initial admin password before any operation."
