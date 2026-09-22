import json
import logging
import logging.handlers
import os
import tempfile
from datetime import datetime, timezone

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
from .lock import BackupLock, lock_key_for_host
from .notify import notify

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"


class _LogCapture(logging.Handler):
    """Collects log records emitted during a single backup run."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


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
    status = load_status()
    status[name] = {**status.get(name, {}), **patch}
    _save_status(status)


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
    way `run_backup()` keys it — see `lock_key_for_host`). `run_backup()`
    grabs that lock before writing "running" and holds it until its final
    status write completes, so a successful acquire here is proof that
    nothing is actively writing to this host's status right now — at which
    point it's safe to check and, if still "running", fix it. A failed
    acquire means a real run holds it; leave that entry alone.

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
            _update_status(name, {
                "status": "error",
                "error": "Interrupted: process restarted while a backup was running.",
            })
            continue

        lock = BackupLock(lock_key_for_host(host_cfg.cpanel_host, host_cfg.ftp_username))
        if not lock.acquire():
            continue
        try:
            current = load_status().get(name, {})
            if current.get("status") == "running":
                _update_status(name, {
                    "status": "error",
                    "error": "Interrupted: process restarted while a backup was running.",
                })
        finally:
            lock.release()


def run_backup(cfg: HostConfig, notifications: dict | None = None) -> dict:
    started = datetime.now(timezone.utc)

    lock = BackupLock(lock_key_for_host(cfg.cpanel_host, cfg.ftp_username))
    if not lock.acquire():
        log.warning("[%s] Skipped: another process is already backing up this host.", cfg.name)
        return {
            "name": cfg.name,
            "status": "skipped",
            "error": "Skipped: another process is already backing up this host.",
            "started": started.isoformat(),
            "ended": started.isoformat(),
            "duration_seconds": 0,
        }

    capture = _LogCapture()
    logging.getLogger().addHandler(capture)

    try:
        _update_status(cfg.name, {"status": "running", "started": started.isoformat(), "error": None})
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
            old_filename = wait_for_backup(cfg.host, cfg.ftp_username, cfg.ftp_password, cfg.time_to_wait, stable_rounds=1)
            old_dest = os.path.join(cfg.destination_folder, old_filename)
            log.info("[%s] Downloading pre-existing %s → %s", cfg.name, old_filename, old_dest)
            download_with_resume(cfg.host, cfg.ftp_username, cfg.ftp_password, old_filename, old_dest)
            delete_file(cfg.host, cfg.ftp_username, cfg.ftp_password, old_filename)
            log.warning("[%s] Pre-existing backup %s saved locally and removed from FTP — requesting fresh backup now.", cfg.name, old_filename)

        if not existing or cfg.request_after_download:
            log.info("[%s] Requesting new backup via cPanel API...", cfg.name)
            if not request_backup(cfg.cpanel_host, cfg.cpanel_username, cfg.cpanel_api_token, cfg.mail_to_notify):
                raise RuntimeError("cPanel backup request failed")
            log.info("[%s] Waiting for new backup file to be ready...", cfg.name)
            filename = wait_for_backup(cfg.host, cfg.ftp_username, cfg.ftp_password, cfg.time_to_wait)
            dest = os.path.join(cfg.destination_folder, filename)
            log.info("[%s] Downloading %s → %s", cfg.name, filename, dest)
            download_with_resume(cfg.host, cfg.ftp_username, cfg.ftp_password, filename, dest)
            delete_file(cfg.host, cfg.ftp_username, cfg.ftp_password, filename)
        else:
            filename = old_filename
            dest = old_dest

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
    return result
