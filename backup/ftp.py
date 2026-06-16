import logging
import os
import time
from ftplib import FTP

from tqdm import tqdm

log = logging.getLogger(__name__)


def connect(host: str, username: str, password: str) -> FTP:
    ftp = FTP(host)
    ftp.login(username, password)
    ftp.cwd("/")
    return ftp


def get_backup_filename(ftp: FTP) -> str | None:
    for name in ftp.nlst():
        if name.startswith("backup-") and name.endswith(".tar.gz"):
            return name
    return None


_MIN_BACKUP_BYTES = 10 * 1024 * 1024  # 10 MB — below this the file is a cPanel placeholder


def wait_for_backup(host: str, username: str, password: str, poll_seconds: int) -> str:
    """Poll until a backup file appears, exceeds the minimum size threshold,
    and its size stabilizes between two consecutive checks."""
    previous_size: int | None = None
    while True:
        try:
            ftp = connect(host, username, password)
            filename = get_backup_filename(ftp)
            if filename:
                size = ftp.size(filename)
                ftp.quit()
                if size < _MIN_BACKUP_BYTES:
                    log.info(
                        "Backup %s found but only %d bytes — cPanel is still initializing, waiting %ds...",
                        filename, size, poll_seconds,
                    )
                    previous_size = None  # reset so a later valid size requires two stable readings
                    time.sleep(poll_seconds)
                    continue
                if size == previous_size:
                    log.info("Size stable at %d bytes — ready to download.", size)
                    return filename
                previous_size = size
                log.info("Backup %s found (%d bytes), waiting %ds for size to stabilize...", filename, size, poll_seconds)
                time.sleep(poll_seconds)
            else:
                ftp.quit()
                log.info("No backup file yet, retrying in 15s...")
                time.sleep(15)
        except Exception as e:
            log.warning("FTP error while polling: %s — retrying in 10s", e)
            time.sleep(10)


def download_with_resume(host: str, username: str, password: str, filename: str, dest_path: str) -> None:
    """Download with automatic resume on failure."""
    while True:
        try:
            ftp = connect(host, username, password)
            remote_size = ftp.size(filename)
            local_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0

            if local_size == remote_size:
                log.info("File already fully downloaded, skipping.")
                ftp.quit()
                return

            with open(dest_path, "ab") as f, tqdm(
                total=remote_size,
                initial=local_size,
                unit="B",
                unit_scale=True,
                desc=filename,
            ) as pbar:
                ftp.retrbinary(
                    f"RETR {filename}",
                    lambda chunk: (f.write(chunk), pbar.update(len(chunk))),
                    rest=local_size,
                )

            ftp.quit()
            log.info("Download complete: %s", dest_path)
            return
        except Exception as e:
            log.warning("Download error: %s — retrying in 10s", e)
            time.sleep(10)


def delete_file(host: str, username: str, password: str, filename: str) -> None:
    ftp = connect(host, username, password)
    ftp.delete(filename)
    ftp.quit()
    log.info("Deleted remote file: %s", filename)
