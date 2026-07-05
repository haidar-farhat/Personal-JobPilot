#!/bin/bash
# Remove JobPilot login autostart on macOS.
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.jobpilot.watchdog.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed LaunchAgent. JobPilot will no longer start at login."
echo "(Any currently running services keep running until you stop them:"
echo "  pkill -f watchdog.py; pkill -f 'uvicorn server.dashboard'; pkill -f scheduler.py)"
