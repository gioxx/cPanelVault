import json
import logging
import logging.handlers
import os
import tempfile
import threading
import time
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
from .notify import notify

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"


# Serializes read-modify-write cycles on STATUS_FILE within this process
# (concurrent host runs, live log mirroring, download progress updates).
_status_lock = threading.RLock()

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

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
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
    with _status_lock:
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


def reconcile_stale_running() -> None:
    """Mark any entry left at status="running" as interrupted.

    A process killed mid-backup (e.g. `docker compose down`) never reaches
    the `finally` block in `run_backup`, so the "running" flag written at
    the start of the run stays on disk forever. On the next startup nothing
    is actually running, but the stale flag masks the last real outcome and
    the dashboard falls back to showing "Never run" instead of the true
    last-known state.
    """
    with _status_lock:
        status = load_status()
        changed = False
        for name, entry in status.items():
            if entry.get("status") == "running":
                entry["status"] = "error"
                entry["error"] = "Interrupted: process restarted while a backup was running."
                entry["phase"] = None
                entry["progress"] = None
                changed = True
        if changed:
            _save_status(status)


def run_backup(cfg: HostConfig, notifications: dict | None = None) -> dict:
    started = datetime.now(timezone.utc)
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

    capture = _LogCapture(cfg.name)
    logging.getLogger().addHandler(capture)

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

    finally:
        logging.getLogger().removeHandler(capture)

    result["name"] = cfg.name
    result["log_lines"] = capture.lines
    result["log_total"] = capture.total
    result["phase"] = None
    result["progress"] = None
    result["current_file"] = None
    _update_status(cfg.name, result)
    notify(notifications or {}, result)
    return result
