"""JobPilot Watchdog — keeps Ollama, the dashboard, and the scheduler alive.

This is the single supervisor process for JobPilot. It starts each service,
health-checks them on a loop, and restarts any that die or stop responding.
Run it once (ideally at login via Task Scheduler) and everything stays up
across crashes — no more manual restarts after a reboot.

    python watchdog.py            # run in foreground (Ctrl-C to stop)

Services supervised:
  - ollama serve        (health: GET http://127.0.0.1:11434/api/tags)
  - dashboard (uvicorn) (health: GET http://127.0.0.1:7777/api/stats)
  - scheduler.py        (no HTTP surface — supervised by process liveness)

Design notes:
  - Ollama + dashboard use HTTP health checks, so the watchdog recovers them
    even if they were started by something else and then wedged.
  - A freshly (re)started service gets a grace window before the next health
    check can flag it again, so we never thrash a service that is just booting.
  - Children are spawned detached with their own log files under ./logs so they
    outlive the shell that launched the watchdog.
"""

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
if sys.platform == "win32":
    VENV_PY = ROOT / "venv" / "Scripts" / "python.exe"
else:
    VENV_PY = ROOT / "venv" / "bin" / "python"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

CHECK_INTERVAL = 30   # seconds between health sweeps
START_GRACE = 30      # seconds a just-started service is left alone to boot

# Windows process-creation flags so children detach from this console.
if sys.platform == "win32":
    CREATE_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
else:
    CREATE_FLAGS = 0

_PY = str(VENV_PY) if VENV_PY.exists() else sys.executable


def _find_ollama() -> str:
    """Resolve the ollama binary — launchd/Task Scheduler PATHs often miss it."""
    found = shutil.which("ollama")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/ollama",                              # macOS arm64 Homebrew
        "/usr/local/bin/ollama",                                 # macOS intel / Linux
        "/Applications/Ollama.app/Contents/Resources/ollama",    # macOS app bundle
        str(Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"),  # Windows
    ):
        if Path(candidate).exists():
            return candidate
    return "ollama"  # last resort — hope PATH has it


# name -> config
SERVICES = {
    "ollama": {
        "cmd": [_find_ollama(), "serve"],
        "health": "http://127.0.0.1:11434/api/tags",
    },
    "dashboard": {
        "cmd": [_PY, "-m", "uvicorn", "server.dashboard:app",
                "--host", "127.0.0.1", "--port", "7777"],
        "health": "http://127.0.0.1:7777/api/stats",
    },
    "scheduler": {
        "cmd": [_PY, "scheduler.py"],
        "health": None,   # no HTTP endpoint — supervised by liveness only
    },
}

# runtime state: name -> {"proc": Popen|None, "started_at": float}
_state: dict[str, dict] = {name: {"proc": None, "started_at": 0.0} for name in SERVICES}


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOGS / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def spawn(name: str) -> None:
    cmd = SERVICES[name]["cmd"]
    logfile = open(LOGS / f"{name}.log", "a", buffering=1, encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=logfile,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_FLAGS,
            close_fds=True,
        )
        _state[name] = {"proc": proc, "started_at": time.time()}
        log(f"START  {name}  (pid={proc.pid})")
    except FileNotFoundError as e:
        log(f"ERROR  could not start {name}: {e}  (cmd={cmd[0]!r} not found on PATH)")
    except Exception as e:
        log(f"ERROR  could not start {name}: {e}")


def kill(name: str) -> None:
    proc = _state[name]["proc"]
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log(f"KILLED {name}  (pid={proc.pid})")
        except Exception as e:
            log(f"WARN   failed to kill {name}: {e}")


def ensure(name: str) -> None:
    cfg = SERVICES[name]
    st = _state[name]
    proc = st["proc"]
    proc_alive = bool(proc and proc.poll() is None)
    booting = proc_alive and (time.time() - st["started_at"] < START_GRACE)

    if cfg["health"] is None:
        # Liveness-only service (scheduler): restart if the process is gone.
        if not proc_alive:
            log(f"DOWN   {name}  (process not running)")
            spawn(name)
        return

    # HTTP-health service (ollama, dashboard).
    if http_ok(cfg["health"]):
        return
    if booting:
        return  # still coming up — don't thrash it
    log(f"DOWN   {name}  (health check failed: {cfg['health']})")
    if proc_alive:
        kill(name)
    spawn(name)


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    log("=" * 52)
    log("JobPilot Watchdog starting — supervising: " + ", ".join(SERVICES))
    log(f"python: {_PY}")
    log(f"check interval: {CHECK_INTERVAL}s   start grace: {START_GRACE}s")
    log("=" * 52)

    try:
        while True:
            for name in SERVICES:
                try:
                    ensure(name)
                except Exception as e:
                    log(f"WARN   ensure({name}) raised: {e}")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        log("Watchdog stopping (Ctrl-C). Leaving services running.")


if __name__ == "__main__":
    main()
