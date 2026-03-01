"""Folder watcher that detects newly created/moved files."""
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _NewFileHandler(FileSystemEventHandler):
    """Watchdog event handler that waits for files to finish writing."""

    SETTLE_SECONDS = 2.0

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self._pending: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._timer_thread = threading.Thread(target=self._settle_loop, daemon=True)
        self._timer_thread.start()

    def on_created(self, event):
        if not event.is_directory:
            self._track(event.src_path)

    def on_moved(self, event):
        """Handle files moved/dragged into the watched folder."""
        if not event.is_directory:
            self._track(event.dest_path)

    def _track(self, filepath):
        try:
            size = Path(filepath).stat().st_size
        except OSError:
            size = 0
        with self._lock:
            self._pending[filepath] = (time.monotonic(), size)

    def _settle_loop(self):
        """Poll pending files until they stop growing, then fire callback."""
        while not self._stop_event.is_set():
            time.sleep(1.0)
            now = time.monotonic()
            ready = []
            with self._lock:
                for filepath, (last_time, last_size) in list(self._pending.items()):
                    if now - last_time < self.SETTLE_SECONDS:
                        continue
                    try:
                        current_size = Path(filepath).stat().st_size
                    except OSError:
                        del self._pending[filepath]
                        continue
                    if current_size == last_size and current_size > 0:
                        ready.append(filepath)
                        del self._pending[filepath]
                    else:
                        # Still being written
                        self._pending[filepath] = (now, current_size)
            for filepath in ready:
                try:
                    self.callback(filepath)
                except Exception:
                    pass

    def stop(self):
        self._stop_event.set()


class FileWatcher:
    """High-level wrapper around a watchdog Observer."""

    def __init__(self, watch_folder: str, callback):
        self.watch_folder = watch_folder
        self.callback = callback
        self._observer: Observer | None = None
        self._handler: _NewFileHandler | None = None

    def start(self):
        self._handler = _NewFileHandler(self.callback)
        self._observer = Observer()
        self._observer.schedule(self._handler, self.watch_folder, recursive=True)
        self._observer.start()

    def stop(self):
        if self._handler:
            self._handler.stop()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        self._observer = None
        self._handler = None
