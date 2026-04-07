"""
Development runner — auto-restarts app.py whenever a .py or .js source file changes.

Usage:
    python dev.py

How it works:
    HyperDiv reads plugin JS at Python import time (not from the browser cache),
    so a full process restart picks up both Python and JS edits.  watchfiles
    watches the project directory and kills/restarts the server on every save.
"""

import subprocess
import sys
from pathlib import Path

from watchfiles import watch

PROJECT_ROOT = Path(__file__).parent

# Only restart on source-code changes — ignore __pycache__, .git, etc.
WATCH_EXTENSIONS = {".py", ".js"}
IGNORE_DIRS = {"__pycache__", ".git", ".venv", "references", "node_modules"}


def _relevant(change, path: str) -> bool:
    p = Path(path)
    if any(part in IGNORE_DIRS for part in p.parts):
        return False
    return p.suffix in WATCH_EXTENSIONS


def _start() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "app.py"], cwd=PROJECT_ROOT)


def main():
    proc = _start()
    print("dev: server started — watching for changes…\n")
    try:
        for _ in watch(PROJECT_ROOT, watch_filter=_relevant):
            print("\n" + "─" * 52)
            print("dev: change detected — restarting server…")
            print("─" * 52 + "\n")
            proc.terminate()
            proc.wait()
            proc = _start()
    except KeyboardInterrupt:
        print("\ndev: shutting down…")
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
