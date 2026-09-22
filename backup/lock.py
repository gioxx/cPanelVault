import logging
import os
import re

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")
LOCK_DIR = os.environ.get("LOCK_DIR", os.path.join(os.path.dirname(os.path.abspath(STATUS_FILE)), "locks"))

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class BackupLockedError(Exception):
    """Raised when a backup lock is already held by another process."""

    def __init__(self, name: str):
        super().__init__(f"Backup for '{name}' is already running (interprocess lock held).")
        self.name = name


def _lock_path(name: str) -> str:
    # Host names come straight from a user-editable config file, so sanitize
    # before using one as a filename: strips path separators and ".." to
    # keep the lock inside LOCK_DIR, and avoids characters invalid on some
    # filesystems.
    safe = _UNSAFE_CHARS.sub("_", name) or "lock"
    return os.path.join(LOCK_DIR, f"{safe}.lock")


class BackupLock:
    """Per-host interprocess mutual exclusion.

    Prevents a CLI run (`main.py backup <host>`) and the web
    scheduler/dashboard from backing up the same host at the same time —
    both share the same status.json and the same remote FTP files, so a
    concurrent run for one host can corrupt status or race on the backup
    file.

    Backed by an OS-level advisory file lock (`filelock`, using
    fcntl/msvcrt under the hood) rather than a hand-rolled heartbeat file:
    the OS releases it automatically the moment the owning process dies or
    is killed — including a hard `docker compose down` mid-run — so there
    is no staleness window, no "steal" logic, and no way for two processes
    to both end up believing they hold the same lock.
    """

    def __init__(self, name: str):
        self.name = name
        os.makedirs(LOCK_DIR, exist_ok=True)
        self._lock = FileLock(_lock_path(name))

    def acquire(self) -> bool:
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            return False
        return True

    def release(self) -> None:
        if self._lock.is_locked:
            self._lock.release()


def is_locked(name: str) -> bool:
    """True if another process currently holds the lock for `name`."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    probe = FileLock(_lock_path(name))
    try:
        probe.acquire(timeout=0)
    except Timeout:
        return True
    probe.release()
    return False
