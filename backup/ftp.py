import logging
import os
import shutil
import time
from ftplib import FTP

from tqdm import tqdm

from . import fmt_size

log = logging.getLogger(__name__)


class InsufficientDiskSpaceError(Exception):
    """Raised when there isn't enough free local disk space to complete a download."""


_FTP_TIMEOUT_SECONDS = 60


def connect(host: str, username: str, password: str) -> FTP:
    ftp = FTP(host, timeout=_FTP_TIMEOUT_SECONDS)
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

            remaining = remote_size - local_size
            free_space = shutil.disk_usage(os.path.dirname(dest_path) or ".").free
            if free_space < remaining:
                ftp.quit()
                raise InsufficientDiskSpaceError(
                    f"Not enough disk space to download {filename}: need {fmt_size(remaining)}, "
                    f"only {fmt_size(free_space)} available."
                )

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
        except InsufficientDiskSpaceError:
            raise
        except Exception as e:
            log.warning("Download error: %s — retrying in 10s", e)
            time.sleep(10)


def _remote_file_missing(host: str, username: str, password: str, filename: str) -> bool:
    """Check whether `filename` is absent from the FTP server.

    Used after an ambiguous delete failure (e.g. a timed-out DELE response)
    to tell a genuine failure apart from a delete that actually succeeded
    server-side but whose confirmation never reached the client.
    Returns False (assume still present) if the check itself can't be
    completed, so callers keep retrying instead of giving up early.
    """
    try:
        ftp = connect(host, username, password)
        try:
            return filename not in ftp.nlst()
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
    except Exception:
        return False


def delete_file(host: str, username: str, password: str, filename: str, max_retries: int = 5) -> None:
    attempt = 0
    while True:
        attempt += 1
        ftp = None
        try:
            ftp = connect(host, username, password)
            ftp.delete(filename)
            log.info("Deleted remote file: %s", filename)
            try:
                ftp.quit()
            except Exception:
                pass
            return
        except Exception as e:
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass
            if _remote_file_missing(host, username, password, filename):
                log.info("Delete for %s timed out but file is already gone on the server — treating as success.", filename)
                return
            if attempt >= max_retries:
                raise
            log.warning("Delete error for %s: %s — retrying in 10s (%d/%d)", filename, e, attempt, max_retries)
            time.sleep(10)
