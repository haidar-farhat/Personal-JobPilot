#!/bin/bash
# JobPilot launcher for macOS — double-click in Finder or run from a terminal.
# Starts the watchdog (which supervises Ollama + dashboard + scheduler)
# and opens the dashboard in your browser.
cd "$(dirname "$0")"

echo
echo "  ====================================================="
echo "   Starting JobPilot Mission Control"
echo "   (watchdog keeps Ollama + Dashboard + Scheduler up)"
echo "  ====================================================="
echo

# If a watchdog is already running, don't start a second one.
if pgrep -f "watchdog.py" > /dev/null 2>&1; then
    echo "  Watchdog already running — opening dashboard."
else
    nohup ./venv/bin/python watchdog.py >> logs/watchdog-launch.log 2>&1 &
    echo "  Watchdog started (pid $!)."
fi

# Give the dashboard a few seconds to bind port 7777, then open it.
for i in $(seq 1 20); do
    if curl -s --max-time 1 http://127.0.0.1:7777/api/stats > /dev/null 2>&1; then
        break
    fi
    sleep 1
done
open http://127.0.0.1:7777

echo
echo "  Mission Control is live:"
echo "    Dashboard:  http://127.0.0.1:7777"
echo "    Logs:       ./logs/"
echo
echo "  Tip: run ./install_autostart_mac.sh once to have this launch"
echo "       automatically every time you log in."
echo
