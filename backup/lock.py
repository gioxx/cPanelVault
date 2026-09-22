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
    # Hostnames are case-insensitive, but `cpanel_host` strips "ftp." with a
    # case-sensitive removeprefix: lowercase first, then strip again so
    # "FTP.example.com" and "example.com" share one key.
    host = cpanel_host.strip().lower().removeprefix("ftp.")
    return f"{host}:{ftp_username.strip()}"


class BackupLockedError(Exception):
    """Raised when a backup lock is already held by another process."""

    def __init__(self, name: str):
        super().__init__(f"Backup for '{name}' is already running (interprocess lock held).")
        self.name = name


def _safe_stem(name: str) -> str:
    # Host names come straight from a user-editable config file, so sanitize
    # before using one as a filename: strips path separators and ".." to
    # keep the lock inside LOCK_DIR, and avoids characters invalid on some
    # filesystems. A collision-prone substitution (e.g. "site/a" and
    # "site.a" both becoming "site_a") would let unrelated hosts share a
    # lock, so a hash of the original name is appended to keep every
    # distinct config key on its own lock file.
    safe = _UNSAFE_CHARS.sub("_", name)[:80]
    digest = hashlib.sha1(name.encode()).hexdigest()[:10]
    return f"{safe}-{digest}"


def _lock_path(name: str) -> str:
    return os.path.join(LOCK_DIR, f"{_safe_stem(name)}.lock")


def _owner_path(name: str) -> str:
    # Kept as a file separate from the FileLock target: on POSIX, filelock
    # opens its lock file with O_TRUNC before attempting flock, so even a
    # *failed* acquire (e.g. a losing contender, or is_locked()'s probe)
    # would wipe out owner metadata stored in that same file.
    return os.path.join(LOCK_DIR, f"{_safe_stem(name)}.owner")


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

    def __init__(self, key: str):
        self.key = key
        self._path = _lock_path(key)
        self._owner_path = _owner_path(key)
        os.makedirs(LOCK_DIR, exist_ok=True)
        self._lock = FileLock(self._path)

    def acquire(self, owner: str | None = None) -> bool:
        for attempt in range(_ACQUIRE_RETRY_ATTEMPTS):
            try:
                self._lock.acquire(timeout=0)
            except Timeout:
                if attempt + 1 < _ACQUIRE_RETRY_ATTEMPTS:
                    time.sleep(_ACQUIRE_RETRY_DELAY_SECONDS)
                continue
            if owner is not None:
                try:
                    with open(self._owner_path, "w") as f:
                        f.write(owner)
                except OSError:
                    pass
            return True
        return False

    def release(self) -> None:
        if not self._lock.is_locked:
            return
        # Remove our owner metadata *before* releasing the OS lock: a
        # successor could acquire the lock and write its own owner file the
        # instant it's freed, and removing it after our own release() could
        # delete that successor's fresh metadata instead of our stale one.
        try:
            os.remove(self._owner_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self._lock.release()


def is_locked(key: str) -> bool:
    """True if another process currently holds the lock for `key`."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    probe = FileLock(_lock_path(key))
    try:
        probe.acquire(timeout=0)
    except Timeout:
        return True
    probe.release()
    return False


class LockOwnerUnknownError(Exception):
    """The current owner of a lock exists but couldn't be read right now.

    On POSIX (flock), a plain read from another process is unaffected by
    who holds the advisory lock, so this shouldn't happen in the deployed
    (Linux) environment. Some platforms' file-locking primitives (e.g.
    Windows' msvcrt, used in local dev) block ordinary reads from other
    processes while a lock is held, though, so callers must treat "can't
    tell" as genuinely unknown rather than assuming no owner.
    """


def current_owner(key: str) -> str | None:
    """The `owner` label passed to the current holder's `acquire()`.

    Multiple config entries can share one lock key when they alias the same
    remote account (see `lock_key_for_host`). A caller holding a stale
    status for one of those aliases needs to know *which* alias actually
    owns a contended lock — the shared lock being held doesn't by itself
    mean this particular alias is the one running.

    Returns None only when there's genuinely no lock file (never acquired,
    or already released). Raises `LockOwnerUnknownError` if the file exists
    but can't be read right now — never silently treat that as "no owner".
    """
    try:
        with open(_owner_path(key)) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None
    except OSError as e:
        raise LockOwnerUnknownError(key) from e
