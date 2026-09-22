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
from .lock import BackupLock, is_locked
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


def reconcile_stale_running() -> None:
    """Mark any entry left at status="running" as interrupted, unless it's genuinely active.

    A process killed mid-backup (e.g. `docker compose down`) never reaches
    the `finally` block in `run_backup`, so the "running" flag written at
    the start of the run stays on disk forever. On the next startup nothing
    is actually running, but the stale flag masks the last real outcome and
    the dashboard falls back to showing "Never run" instead of the true
    last-known state.

    A "running" entry is left untouched when its host still holds a live
    interprocess lock (see `.lock`) — that means a separate process (e.g. a
    CLI run) genuinely has that backup in progress right now.
    """
    status = load_status()
    changed = False
    for name, entry in status.items():
        if entry.get("status") == "running" and not is_locked(name):
            entry["status"] = "error"
            entry["error"] = "Interrupted: process restarted while a backup was running."
            changed = True
    if changed:
        _save_status(status)


def run_backup(cfg: HostConfig, notifications: dict | None = None) -> dict:
    started = datetime.now(timezone.utc)

    lock = BackupLock(cfg.name)
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
