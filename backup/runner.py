import json
import logging
import os
from datetime import datetime, timezone

from .cleaner import clean_old_backups
from .config import HostConfig
from .cpanel import request_backup
from .ftp import connect, delete_file, download_with_resume, get_backup_filename, wait_for_backup
from .notify import notify

log = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("STATUS_FILE", "/data/status.json")


def load_status() -> dict:
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {}


def _save_status(data: dict) -> None:
    parent = os.path.dirname(STATUS_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _update_status(name: str, patch: dict) -> None:
    status = load_status()
    status[name] = {**status.get(name, {}), **patch}
    _save_status(status)


def run_backup(cfg: HostConfig, notifications: dict | None = None) -> dict:
    started = datetime.now(timezone.utc)
    _update_status(cfg.name, {"status": "running", "started": started.isoformat(), "error": None})

    try:
        os.makedirs(cfg.destination_folder, exist_ok=True)

        ftp = connect(cfg.host, cfg.ftp_username, cfg.ftp_password)
        existing = get_backup_filename(ftp)
        ftp.quit()

        if not existing:
            log.info("[%s] Requesting new backup via cPanel API...", cfg.name)
            if not request_backup(cfg.cpanel_host, cfg.cpanel_username, cfg.cpanel_api_token, cfg.mail_to_notify):
                raise RuntimeError("cPanel backup request failed")

        log.info("[%s] Waiting for backup file to be ready...", cfg.name)
        filename = wait_for_backup(cfg.host, cfg.ftp_username, cfg.ftp_password, cfg.time_to_wait)

        dest = os.path.join(cfg.destination_folder, filename)
        log.info("[%s] Downloading %s → %s", cfg.name, filename, dest)
        download_with_resume(cfg.host, cfg.ftp_username, cfg.ftp_password, filename, dest)

        delete_file(cfg.host, cfg.ftp_username, cfg.ftp_password, filename)

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
        log.info("[%s] Done: %s (%d bytes)", cfg.name, filename, result["size_bytes"])

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
    _update_status(cfg.name, result)
    notify(notifications or {}, result)
    return result
