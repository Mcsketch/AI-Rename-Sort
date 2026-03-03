"""
watch_build.py — Auto-rebuilds the PyInstaller executable whenever source files change.

Usage:
    python watch_build.py          # watch & auto-build
    python watch_build.py --once   # single build then exit
"""

import subprocess
import sys
import time
import argparse
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

ROOT = Path(__file__).parent
SPEC_FILE = ROOT / "AI-Rename-Sort.spec"
WATCH_DIRS = [ROOT / "src", ROOT]
WATCH_EXTS = {".py", ".spec"}
DEBOUNCE_SECONDS = 2.0


def build() -> bool:
    """Run PyInstaller and return True on success."""
    print("\n>>> Building executable...", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", str(SPEC_FILE)],
        cwd=ROOT,
    )
    if result.returncode == 0:
        print(">>> Build succeeded.\n", flush=True)
        return True
    else:
        print(f">>> Build FAILED (exit code {result.returncode}).\n", flush=True)
        return False


class RebuildHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self._pending = False
        self._last_trigger = 0.0

    def on_modified(self, event):
        self._schedule(event.src_path)

    def on_created(self, event):
        self._schedule(event.src_path)

    def _schedule(self, path: str):
        if Path(path).suffix in WATCH_EXTS and "_pycache_" not in path:
            self._last_trigger = time.monotonic()
            self._pending = True

    def consume_pending(self) -> bool:
        if self._pending and (time.monotonic() - self._last_trigger) >= DEBOUNCE_SECONDS:
            self._pending = False
            return True
        return False


def watch():
    handler = RebuildHandler()
    observer = Observer()

    for watch_dir in WATCH_DIRS:
        if watch_dir.is_dir():
            observer.schedule(handler, str(watch_dir), recursive=True)

    observer.start()
    print(f"Watching for changes in: {', '.join(str(d) for d in WATCH_DIRS if d.is_dir())}")
    print("Press Ctrl+C to stop.\n")

    # Initial build on startup
    build()

    try:
        while True:
            time.sleep(0.5)
            if handler.consume_pending():
                build()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        print("Watcher stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or watch-and-build the executable.")
    parser.add_argument("--once", action="store_true", help="Build once and exit.")
    args = parser.parse_args()

    if args.once:
        success = build()
        sys.exit(0 if success else 1)
    else:
        watch()
