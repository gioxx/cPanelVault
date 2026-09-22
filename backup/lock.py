import hashlib
import logging
import os
import re
import time

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")
LOCK_DIR = os.environ.get("LOCK_DIR", os.path.join(os.path.dirname(os.path.abspath(STATUS_FILE)), "locks"))

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")

# is_locked() briefly takes and releases the same OS lock just to probe it,
# which can momentarily collide with a real acquire() happening at the same
# instant. A couple of short retries absorb that without weakening mutual
# exclusion for a genuinely concurrent backup, whose lock is held for
# minutes, not milliseconds.
_ACQUIRE_RETRY_ATTEMPTS = 3
_ACQUIRE_RETRY_DELAY_SECONDS = 0.05


def lock_key_for_host(cpanel_host: str, ftp_username: str) -> str:
    """Identity to lock on for a given remote account.

    Two config entries with different display names can still point at the
    same cPanel/FTP account (same host + credentials); locking by the
    config key alone would let them run concurrently and race on the same
    remote backup file. Callers must pass the *canonical* cPanel host (i.e.
    `HostConfig.cpanel_host`, which already strips an "ftp." prefix) rather
    than the raw FTP hostname, so "ftp.example.com" and "example.com" — the
    same account — collapse onto the same lock instead of two different ones.
    """
    return f"{cpanel_host.strip().lower()}:{ftp_username.strip()}"


class BackupLockedError(Exception):
    """Raised when a backup lock is already held by another process."""

    def __init__(self, name: str):
        super().__init__(f"Backup for '{name}' is already running (interprocess lock held).")
        self.name = name


def _lock_path(name: str) -> str:
    # Host names come straight from a user-editable config file, so sanitize
    # before using one as a filename: strips path separators and ".." to
    # keep the lock inside LOCK_DIR, and avoids characters invalid on some
    # filesystems. A collision-prone substitution (e.g. "site/a" and
    # "site.a" both becoming "site_a") would let unrelated hosts share a
    # lock, so a hash of the original name is appended to keep every
    # distinct config key on its own lock file.
    safe = _UNSAFE_CHARS.sub("_", name)[:80]
    digest = hashlib.sha1(name.encode()).hexdigest()[:10]
    return os.path.join(LOCK_DIR, f"{safe}-{digest}.lock")


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
        for attempt in range(_ACQUIRE_RETRY_ATTEMPTS):
            try:
                self._lock.acquire(timeout=0)
            except Timeout:
                if attempt + 1 < _ACQUIRE_RETRY_ATTEMPTS:
                    time.sleep(_ACQUIRE_RETRY_DELAY_SECONDS)
                continue
            return True
        return False

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
