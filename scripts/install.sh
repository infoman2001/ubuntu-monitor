#!/usr/bin/env bash
# Deploy a checkout as an isolated release. Run with sudo; Git stays owned by infoman.
set -Eeuo pipefail
umask 077
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=/opt/ubuntu-monitor
UNITS=/etc/systemd/system
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash scripts/install.sh' >&2; exit 1; }
for tool in python3 systemctl systemd-analyze flock; do
    command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
exec 9>/run/lock/ubuntu-monitor-install.lock
flock -n 9 || { echo 'Another installation is running' >&2; exit 1; }
install -d -m 755 "$TARGET" "$TARGET/releases"
if [[ ! -f $TARGET/.env ]]; then
    install -m 600 "$SOURCE/.env.example" "$TARGET/.env"
    echo "Created $TARGET/.env. Fill in relay credentials, then run this script again."
    exit 0
fi
# Validate configuration without displaying values or contacting the relay.
python3 - "$SOURCE" "$TARGET" <<'PY'
import importlib.util
import os
import sys
import types
from pathlib import Path
sys.modules['psutil'] = types.ModuleType('psutil')
spec = importlib.util.spec_from_file_location('monitor', Path(sys.argv[1]) / 'monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
# Read the file using the same parser, independent of the caller's environment.
for key in ('RELAY_URL', 'RELAY_AUTH', 'TG_TOKEN', 'TG_CHAT_ID'):
    os.environ.pop(key, None)
monitor.load_env(Path(sys.argv[2]) / '.env')
for key in ('RELAY_URL', 'RELAY_AUTH', 'TG_TOKEN', 'TG_CHAT_ID'):
    value = os.getenv(key, '')
    if not value or 'REPLACE_WITH_' in value or 'example.com' in value:
        raise SystemExit(f'Configure {key} in /opt/ubuntu-monitor/.env')
PY
RELEASE=$(mktemp -d "$TARGET/releases/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
SWITCHED=0
COMMITTED=0
BACKUP=''
OLD_CURRENT=$(readlink "$TARGET/current" || true)
TIMER_ACTIVE=0
TIMER_ENABLED=0
systemctl is-active --quiet ubuntu-monitor.timer && TIMER_ACTIVE=1
systemctl is-enabled --quiet ubuntu-monitor.timer 2>/dev/null && TIMER_ENABLED=1
stop_if_present() {
    local load_state
    load_state=$(systemctl show "$1" -p LoadState --value) || return
    [[ -n $load_state ]] || { echo "Cannot determine LoadState of $1" >&2; return 1; }
    # A fresh installation has no units yet. Errors stopping an existing unit
    # must still abort deployment instead of being silently ignored.
    if [[ $load_state != not-found ]]; then systemctl stop "$1"; fi
}
cleanup() {
    local status=$?
    trap - EXIT
    if (( SWITCHED && ! COMMITTED )); then
        set +e
        echo 'Deployment failed; restoring previous release and units.' >&2
        stop_if_present ubuntu-monitor.timer
        stop_if_present ubuntu-monitor.service
        if [[ -n $OLD_CURRENT ]]; then
            ln -sfn "$OLD_CURRENT" "$TARGET/current.next"
            mv -Tf "$TARGET/current.next" "$TARGET/current"
        else
            rm -f "$TARGET/current"
        fi
        for unit in ubuntu-monitor.service ubuntu-monitor.timer; do
            if [[ -f $BACKUP/$unit ]]; then
                cp -a "$BACKUP/$unit" "$UNITS/$unit"
            else
                rm -f "$UNITS/$unit"
            fi
        done
        systemctl daemon-reload
        if (( TIMER_ENABLED )); then
            systemctl enable ubuntu-monitor.timer
        elif [[ $(systemctl show ubuntu-monitor.timer -p LoadState --value) != not-found ]]; then
            systemctl disable ubuntu-monitor.timer
        fi
        if (( TIMER_ACTIVE )); then systemctl start ubuntu-monitor.timer; fi
    fi
    if (( ! COMMITTED )); then rm -rf -- "$RELEASE"; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
install -m 644 "$SOURCE/monitor.py" "$SOURCE/requirements.txt" "$RELEASE/"
# A venv must be created at its final path: moving it breaks pip shebangs.
python3 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/python" -m pip install --disable-pip-version-check -r "$RELEASE/requirements.txt"
"$RELEASE/venv/bin/python" -m pip check
"$RELEASE/venv/bin/python" -m py_compile "$RELEASE/monitor.py"
"$RELEASE/venv/bin/python" -c 'import psutil; psutil.virtual_memory(); psutil.disk_usage("/")'
if command -v git >/dev/null && git -c safe.directory="$SOURCE" -C "$SOURCE" rev-parse HEAD >/dev/null 2>&1; then
    git -c safe.directory="$SOURCE" -C "$SOURCE" rev-parse HEAD > "$RELEASE/REVISION"
fi
chmod 755 "$RELEASE"
BACKUP=/var/backups/ubuntu-monitor/$(basename "$RELEASE")
install -d -m 700 "$BACKUP"
for unit in ubuntu-monitor.service ubuntu-monitor.timer; do
    if [[ -e $UNITS/$unit ]]; then cp -a "$UNITS/$unit" "$BACKUP/$unit"; fi
done
printf '%s\n' "$OLD_CURRENT" > "$BACKUP/previous-release"
SWITCHED=1
stop_if_present ubuntu-monitor.timer
stop_if_present ubuntu-monitor.service
chmod 600 "$TARGET/.env"
ln -s "$RELEASE" "$TARGET/current.next"
mv -Tf "$TARGET/current.next" "$TARGET/current"
install -m 644 "$SOURCE/ubuntu-monitor.service" "$SOURCE/ubuntu-monitor.timer" "$UNITS/"
systemd-analyze verify "$UNITS/ubuntu-monitor.service" "$UNITS/ubuntu-monitor.timer"
systemctl daemon-reload
systemctl enable --now ubuntu-monitor.timer
systemctl is-active --quiet ubuntu-monitor.timer
COMMITTED=1
echo "Installed: $RELEASE"
echo 'Configuration and state preserved. Next check runs via systemd timer.'
echo 'To check immediately: sudo systemctl start ubuntu-monitor.service'
