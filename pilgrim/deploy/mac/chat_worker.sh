#!/usr/bin/env bash
# The Sheep Pen Mac worker (SHEEP_PEN.md §0.5.1, §0.7.7): Layer B review,
# Giles dispatches, the 7-day purge and the worker heartbeat. Its own launchd
# unit beside the station; it never runs inside pilgrim.server.
#
#   pilgrim/deploy/mac/chat_worker.sh install    # render + install launchd unit
#   pilgrim/deploy/mac/chat_worker.sh status
#   pilgrim/deploy/mac/chat_worker.sh uninstall
#
# Secrets stay in the repo-root .env (mode 600); this script never reads or
# prints them. Logs go to ~/Library/Logs/pilgrim/chat_worker.log.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY="$ROOT/.venv/bin/python"
LOGS="$HOME/Library/Logs/pilgrim"
AGENTS="$HOME/Library/LaunchAgents"
LABEL="farm.pilgrim.chat-worker"
PLIST="$AGENTS/$LABEL.plist"
UID_=$(id -u)

render() {
    mkdir -p "$LOGS" "$AGENTS"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string>
    <string>pilgrim.chat_worker</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>NO_COLOR</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/chat_worker.log</string>
  <key>StandardErrorPath</key><string>$LOGS/chat_worker.err</string>
</dict>
</plist>
PLIST
    plutil -lint "$PLIST" >/dev/null
}

install_unit() {
    render
    launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_" "$PLIST"
    launchctl kickstart -k "gui/$UID_/$LABEL"
    echo "installed $LABEL (logs: $LOGS/chat_worker.log)"
}

status() {
    launchctl print "gui/$UID_/$LABEL" 2>/dev/null | sed -n '1,20p' || {
        echo "$LABEL is not loaded"; exit 1; }
    "$ROOT/.venv/bin/python" - "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pilgrim.config import load_config, load_secret
from pilgrim.hotstate import REDIS_URL_ENV
print("REDIS_URL set:", bool(load_secret(REDIS_URL_ENV)))
PY
}

uninstall_unit() {
    launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
}

case "${1:-status}" in
    install) install_unit ;;
    status) status ;;
    uninstall) uninstall_unit ;;
    *) echo "usage: $0 install|status|uninstall" >&2; exit 2 ;;
esac
