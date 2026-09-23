import json
import logging
import logging.handlers
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

from filelock import FileLock

from . import fmt_size
from .cleaner import clean_old_backups
from .config import HostConfig
from .cpanel import request_backup
from .ftp import (
    connect,
    delete_file,
    download_with_resume,
    get_backup_filename,
    wait_for_backup,
)
from .lock import BackupLock, LockOwnerUnknownError, current_owner, lock_key_for_host
from .notify import notify

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")

# Serializes every read-modify-write of STATUS_FILE across processes (CLI
# and web) and threads (concurrent host runs, live log mirroring, download
# progress updates). Reentrant, so a holder can call _update_status().
_status_lock = FileLock(STATUS_FILE + ".lock")

# How long reconciliation waits for a new lock holder to publish its owner.
_OWNER_WAIT_SECONDS = 2


def _locked_status() -> FileLock:
    """`_status_lock`, after making sure its directory exists: FileLock
    can't create the lock file in a missing directory, and this runs
    before `_save_status()` gets a chance to create it."""
    os.makedirs(os.path.dirname(os.path.abspath(STATUS_FILE)), exist_ok=True)
    return _status_lock

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"

# Retry loops (FTP outage, download errors) can log indefinitely: keep only
# the tail so status.json and the dashboard payload stay bounded.
_MAX_LOG_LINES = 500


class _LogCapture(logging.Handler):
    """Collects log records emitted by a single backup run and mirrors them
    live to status.json so the web UI can follow the run as it happens.

    Only records from the thread that started the run are captured, so
    concurrent runs for different hosts don't mix their lines.
    """

    def __init__(self, host: str) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE))
        self.host = host
        self.thread_id = threading.get_ident()
        self.lines: list[str] = []
        self.total = 0
        self._emitting = False

    def emit(self, record: logging.LogRecord) -> None:
        # Mirroring a line takes the status FileLock, which itself logs (at
        # DEBUG) on acquire/release: skip those, and anything logged while a
        # mirror write is in progress, so a record can't recurse into emit().
        if record.thread != self.thread_id or self._emitting or record.name.startswith("filelock"):
            return
        self._emitting = True
        try:
            self.lines.append(self.format(record))
            self.total += 1
            if len(self.lines) > _MAX_LOG_LINES:
                del self.lines[:-_MAX_LOG_LINES]
            _update_status(self.host, {
                "log_lines": self.lines,
                "log_total": self.total,
                "last_message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)
        finally:
            self._emitting = False


def load_status() -> dict:
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {}


def _save_status(data: dict) -> None:
    parent = os.path.dirname(os.path.abspath(STATUS_FILE))
    os.makedirs(parent, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=parent, delete=False, suffix=".tmp") as tmp:
        json.dump(data, tmp, indent=2, default=str)
        tmp_path = tmp.name
    os.replace(tmp_path, STATUS_FILE)


def _update_status(name: str, patch: dict) -> None:
    with _locked_status():
        status = load_status()
        status[name] = {**status.get(name, {}), **patch}
        _save_status(status)


def _set_phase(name: str, phase: str | None, current_file: str | None = None) -> None:
    # Live-UI metadata only: a failing status write (full or flaky volume)
    # must not abort the backup itself.
    try:
        _update_status(name, {"phase": phase, "progress": None, "current_file": current_file})
    except Exception as e:
        log.debug("[%s] Could not record phase %s (ignored): %s", name, phase, e)


# Window over which download speed is averaged: long enough to smooth FTP
# bursts, short enough to follow real changes in throughput.
_SPEED_WINDOW_SECONDS = 30


def _progress_updater(name: str):
    samples: list[tuple[float, int]] = []

    def update(done: int, total: int) -> None:
        now = time.monotonic()
        if samples and done < samples[-1][1]:
            samples.clear()  # restarted from scratch
        samples.append((now, done))
        while len(samples) > 2 and now - samples[0][0] > _SPEED_WINDOW_SECONDS:
            samples.pop(0)
        t0, d0 = samples[0]
        speed = (done - d0) / (now - t0) if now > t0 and done > d0 else None
        _update_status(name, {"progress": {"done": done, "total": total, "speed": speed}})
    return update


_INTERRUPTED = {
    "status": "error",
    "error": "Interrupted: process restarted while a backup was running.",
    "phase": None,
    "progress": None,
    "current_file": None,
}


def reconcile_stale_running(cfg: dict[str, HostConfig]) -> None:
    """Mark any entry left at status="running" as interrupted, unless it's genuinely active.

    A process killed mid-backup (e.g. `docker compose down`) never reaches
    the `finally` block in `run_backup`, so the "running" flag written at
    the start of the run stays on disk forever. On the next startup nothing
    is actually running, but the stale flag masks the last real outcome and
    the dashboard falls back to showing "Never run" instead of the true
    last-known state.

    Checking `is_locked()` and then writing separately would still leave a
    gap: a genuinely active run could finish and persist its own result in
    between, and the "interrupted" write would clobber it. Instead, this
    tries to *acquire* each stale-looking host's own lock (keyed the same
    way `run_backup()` keys it — see `lock_key_for_host`), passing its own
    config name as the owner. `run_backup()` grabs that lock the same way
    before writing "running" and holds it until its final status write
    completes, so a successful acquire here is proof that nothing is
    actively writing to this host's status right now — at which point it's
    safe to check and, if still "running", fix it.

    A failed acquire means *some* run currently holds the lock — but two
    config entries can alias the same remote account and therefore share a
    lock key (see `lock_key_for_host`). If a different alias is the one
    holding it, that alone doesn't mean *this* entry is active: this
    entry's own run_backup() would still be blocked from starting, and
    would leave its stale "running" status untouched otherwise. So on a
    failed acquire, `current_owner()` is checked: this entry is only left
    alone when it is itself the recorded owner.

    That check and the write happen together under the status-file lock.
    Otherwise the other alias could release the shared lock and this entry
    start a real run (writing "running") between the check and the write,
    which would then clobber a live run. `run_backup()` records its owner
    before writing "running", and that write also takes the status-file
    lock, so an owner other than this entry *under that lock* proves this
    entry hasn't written a fresh "running".

    `cfg` maps config keys to their `HostConfig`, needed to derive the same
    lock key `run_backup()` uses. An entry whose config key no longer
    exists can't collide with anything `run_backup()` locks, so it's fixed
    unconditionally.
    """
    status = load_status()
    for name, entry in status.items():
        if entry.get("status") != "running":
            continue

        host_cfg = cfg.get(name)
        if host_cfg is None:
            _update_status(name, _INTERRUPTED)
            continue

        key = lock_key_for_host(host_cfg.host, host_cfg.cpanel_username)
        lock = BackupLock(key)
        if not lock.acquire(owner=name):
            with _locked_status():
                try:
                    # A holder that just acquired may not have published its
                    # owner yet (stale metadata from a dead holder reads as
                    # unknown): give it a moment before giving up.
                    owner = current_owner(key, wait_seconds=_OWNER_WAIT_SECONDS)
                except LockOwnerUnknownError:
                    # Can't tell who holds it right now -- be conservative and
                    # leave this entry as "running" rather than risk
                    # reclassifying a genuinely active run for this alias.
                    continue
                if owner == name:
                    continue
                # A different alias for the same account holds the lock, so
                # this entry's "running" is stale (see docstring).
                if load_status().get(name, {}).get("status") == "running":
                    _update_status(name, _INTERRUPTED)
            continue
        try:
            current = load_status().get(name, {})
            if current.get("status") == "running":
                _update_status(name, _INTERRUPTED)
        finally:
            lock.release()


def run_backup(cfg: HostConfig, notifications: dict | None = None) -> dict:
    started = datetime.now(timezone.utc)

    lock = BackupLock(lock_key_for_host(cfg.host, cfg.cpanel_username))
    if not lock.acquire(owner=cfg.name):
        log.warning("[%s] Skipped: another process is already backing up this host.", cfg.name)
        return {
            "name": cfg.name,
            "status": "skipped",
            "error": "Skipped: another process is already backing up this host.",
            "started": started.isoformat(),
            "ended": started.isoformat(),
            "duration_seconds": 0,
        }

    capture = _LogCapture(cfg.name)
    logging.getLogger().addHandler(capture)

    try:
        _update_status(cfg.name, {
            "status": "running",
            "started": started.isoformat(),
            "error": None,
            "phase": "checking",
            "progress": None,
            "log_lines": [],
            "log_total": 0,
            "last_message": None,
        })
        result = _do_backup(cfg, started, capture)
        _update_status(cfg.name, result)
    finally:
        logging.getLogger().removeHandler(capture)
        lock.release()

    notify(notifications or {}, result)
    return result


def _do_backup(cfg: HostConfig, started: datetime, capture: "_LogCapture") -> dict:
    try:
        os.makedirs(cfg.destination_folder, exist_ok=True)

        ftp = connect(cfg.host, cfg.ftp_username, cfg.ftp_password)
        existing = get_backup_filename(ftp)
        ftp.quit()

        if existing:
            log.warning("[%s] Pre-existing backup found on FTP: %s — downloading it before requesting a fresh one.", cfg.name, existing)
            _set_phase(cfg.name, "existing_wait")
            old_filename = wait_for_backup(cfg.host, cfg.ftp_username, cfg.ftp_password, cfg.time_to_wait, stable_rounds=1)
            old_dest = os.path.join(cfg.destination_folder, old_filename)
            log.info("[%s] Downloading pre-existing %s → %s", cfg.name, old_filename, old_dest)
            _set_phase(cfg.name, "existing_download", old_filename)
            download_with_resume(cfg.host, cfg.ftp_username, cfg.ftp_password, old_filename, old_dest, _progress_updater(cfg.name))
            delete_file(cfg.host, cfg.ftp_username, cfg.ftp_password, old_filename)
            log.warning("[%s] Pre-existing backup %s saved locally and removed from FTP — requesting fresh backup now.", cfg.name, old_filename)

        if not existing or cfg.request_after_download:
            log.info("[%s] Requesting new backup via cPanel API...", cfg.name)
            _set_phase(cfg.name, "requesting")
            if not request_backup(cfg.cpanel_host, cfg.cpanel_username, cfg.cpanel_api_token, cfg.mail_to_notify):
                raise RuntimeError("cPanel backup request failed")
            log.info("[%s] Waiting for new backup file to be ready...", cfg.name)
            _set_phase(cfg.name, "waiting")
            filename = wait_for_backup(cfg.host, cfg.ftp_username, cfg.ftp_password, cfg.time_to_wait)
            dest = os.path.join(cfg.destination_folder, filename)
            log.info("[%s] Downloading %s → %s", cfg.name, filename, dest)
            _set_phase(cfg.name, "downloading", filename)
            download_with_resume(cfg.host, cfg.ftp_username, cfg.ftp_password, filename, dest, _progress_updater(cfg.name))
            delete_file(cfg.host, cfg.ftp_username, cfg.ftp_password, filename)
        else:
            filename = old_filename
            dest = old_dest

        _set_phase(cfg.name, "cleaning")
        removed = clean_old_backups(cfg.destination_folder, cfg.retention_days)

        ended = datetime.now(timezone.utc)
        result = {
            "status": "success",
            "file": filename,
            "size_bytes": os.path.getsize(dest),
            "started": started.isoformat(),
            "ended": ended.isoformat(),
            "duration_seconds": int((ended - started).total_seconds()),
            "cleaned": len(removed),
            "error": None,
        }
        log.info("[%s] Done: %s (%s)", cfg.name, filename, fmt_size(result["size_bytes"]))

    except Exception as e:
        ended = datetime.now(timezone.utc)
        result = {
            "status": "error",
            "error": str(e),
            "started": started.isoformat(),
            "ended": ended.isoformat(),
            "duration_seconds": int((ended - started).total_seconds()),
        }
        log.error("[%s] Failed: %s", cfg.name, e)

    result["name"] = cfg.name
    result["log_lines"] = capture.lines
    result["log_total"] = capture.total
    result["phase"] = None
    result["progress"] = None
    result["current_file"] = None
    return result
