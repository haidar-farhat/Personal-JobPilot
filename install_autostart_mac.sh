#!/bin/bash
# Install JobPilot login autostart on macOS (LaunchAgent, no admin required).
# The watchdog starts at every login and keeps Ollama + dashboard + scheduler up.
# Undo with: ./uninstall_autostart_mac.sh
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PLIST="$HOME/Library/LaunchAgents/com.jobpilot.watchdog.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jobpilot.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>${ROOT}/venv/bin/python</string>
        <string>${ROOT}/watchdog.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${ROOT}/logs/launchagent.log</string>
    <key>StandardErrorPath</key>
    <string>${ROOT}/logs/launchagent.log</string>
</dict>
</plist>
EOF

# Reload cleanly if it was already installed.
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed LaunchAgent: $PLIST"
echo "JobPilot watchdog is now running and will start at every login."
echo "Dashboard: http://127.0.0.1:7777"
