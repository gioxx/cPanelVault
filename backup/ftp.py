import logging
import os
import time
from ftplib import FTP

from tqdm import tqdm

from . import fmt_size

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


_STABLE_ROUNDS_REQUIRED = 3  # consecutive polls with identical size before download
_MIN_BACKUP_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB — reject placeholder/empty files


def wait_for_backup(
    host: str,
    username: str,
    password: str,
    poll_seconds: int,
    stable_rounds: int = _STABLE_ROUNDS_REQUIRED,
    min_size_bytes: int = _MIN_BACKUP_SIZE_BYTES,
) -> str:
    """Poll until a backup file appears, is at least `min_size_bytes` large, and
    its size is identical for `stable_rounds` consecutive checks."""
    previous_size: int | None = None
    stable_count = 0
    while True:
        try:
            ftp = connect(host, username, password)
            filename = get_backup_filename(ftp)
            if filename:
                size = ftp.size(filename)
                ftp.quit()
                if size < min_size_bytes:
                    log.info(
                        "Backup %s: size %s is below minimum %s — waiting for cPanel to write the archive...",
                        filename, fmt_size(size), fmt_size(min_size_bytes),
                    )
                    previous_size = None
                    stable_count = 0
                    time.sleep(poll_seconds)
                    continue
                if size == previous_size:
                    stable_count += 1
                    log.info(
                        "Backup %s: size stable at %s (%d/%d)...",
                        filename, fmt_size(size), stable_count, stable_rounds,
                    )
                    if stable_count >= stable_rounds:
                        log.info("Size confirmed stable — ready to download.")
                        return filename
                else:
                    if previous_size is not None:
                        log.info("Backup %s: size changed %s → %s, resetting counter.", filename, fmt_size(previous_size), fmt_size(size))
                    else:
                        log.info("Backup %s found (%s), starting stability check...", filename, fmt_size(size))
                    previous_size = size
                    stable_count = 0
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
