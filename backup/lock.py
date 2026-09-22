import json
import logging
import os
import socket
import tempfile
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")
LOCK_DIR = os.environ.get("LOCK_DIR", os.path.join(os.path.dirname(os.path.abspath(STATUS_FILE)), "locks"))

HEARTBEAT_INTERVAL_SECONDS = 30
STALE_AFTER_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 4


class BackupLockedError(Exception):
    """Raised when a backup lock is already held by another process."""

    def __init__(self, name: str):
        super().__init__(f"Backup for '{name}' is already running (interprocess lock held).")
        self.name = name


def _lock_path(name: str) -> str:
    return os.path.join(LOCK_DIR, f"{name}.lock")


def _read_lock(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_lock(path: str, data: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=parent, delete=False, suffix=".tmp") as tmp:
        json.dump(data, tmp)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _is_stale(lock: dict) -> bool:
    try:
        updated = datetime.fromisoformat(lock["updated"])
    except (KeyError, ValueError, TypeError):
        return True
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age > STALE_AFTER_SECONDS


def is_locked(name: str) -> bool:
    """True if another process currently holds a live (non-stale) lock for `name`."""
    lock = _read_lock(_lock_path(name))
    return lock is not None and not _is_stale(lock)


class BackupLock:
    """Per-host interprocess lease, backed by a heartbeat file.

    Prevents a CLI run (`main.py backup <host>`) and the web
    scheduler/dashboard from backing up the same host at the same time —
    both share the same status.json and the same remote FTP files, so a
    concurrent run for one host can corrupt status or race on the backup
    file. A lock is only ever stolen once its heartbeat hasn't been
    refreshed for STALE_AFTER_SECONDS, meaning the owning process almost
    certainly died without releasing it (e.g. `docker compose down` mid-run).
    """

    def __init__(self, name: str):
        self.name = name
        self.path = _lock_path(name)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._acquired = False

    def acquire(self, max_attempts: int = 5) -> bool:
        """Atomically claim the lock file via O_CREAT|O_EXCL.

        If the file already exists and is stale, remove it and retry. Two
        processes racing to steal the same stale lock both attempt the
        remove-then-create-exclusive sequence, but O_EXCL guarantees only
        one of them ends up actually holding the file — the loser sees
        EEXIST again on its next attempt and reads a fresh, non-stale lock.
        """
        os.makedirs(LOCK_DIR, exist_ok=True)
        for _ in range(max_attempts):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                existing = _read_lock(self.path)
                if existing is not None and not _is_stale(existing):
                    return False
                if existing is not None:
                    log.warning(
                        "[%s] Stealing stale backup lock (no heartbeat for over %ss, owner pid %s).",
                        self.name, STALE_AFTER_SECONDS, existing.get("pid"),
                    )
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                continue
            else:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._payload(), f)
                self._acquired = True
                self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                self._thread.start()
                return True
        return False

    def _payload(self) -> dict:
        return {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "updated": datetime.now(timezone.utc).isoformat(),
        }

    def _write_heartbeat(self) -> None:
        _write_lock(self.path, self._payload())

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            self._write_heartbeat()

    def release(self) -> None:
        if not self._acquired:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        self._acquired = False
