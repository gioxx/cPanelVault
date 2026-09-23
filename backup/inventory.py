"""Read-only view of the local backup archives, used by the web UI."""

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime

# cPanel names archives backup-<M>.<D>.<YYYY>_<HH>-<MM>-<SS>_<user>.tar.gz
_NAME_RE = re.compile(r"^backup-(\d{1,2})\.(\d{1,2})\.(\d{4})_(\d{2})-(\d{2})-(\d{2})_(.+)\.tar\.gz$")


@dataclass
class BackupFile:
    name: str
    path: str
    size_bytes: int
    mtime: float  # what the retention cleanup compares against
    created: datetime | None  # cPanel's timestamp from the file name (server-local, naive)
    account: str | None


def parse_backup_name(name: str) -> tuple[datetime | None, str | None]:
    m = _NAME_RE.match(name)
    if not m:
        return None, None
    month, day, year, hh, mm, ss, account = m.groups()
    try:
        return datetime(int(year), int(month), int(day), int(hh), int(mm), int(ss)), account
    except ValueError:
        return None, account


def list_backups(folder: str) -> list[BackupFile]:
    """Archives under `folder`, newest first. Mirrors the files that
    `clean_old_backups` would consider (recursive, *.tar.gz)."""
    if not os.path.isdir(folder):
        return []
    found: list[BackupFile] = []
    for root, _, files in os.walk(folder):
        for name in files:
            if not name.endswith(".tar.gz"):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue  # removed between listing and stat
            created, account = parse_backup_name(name)
            found.append(BackupFile(name, path, st.st_size, st.st_mtime, created, account))
    found.sort(key=lambda b: b.mtime, reverse=True)
    return found


def volume_usage(folder: str) -> tuple[int, shutil._ntuple_diskusage] | None:
    """(device id, disk usage) of the filesystem holding `folder`, walking up
    to the nearest existing parent when the folder isn't created yet."""
    path = os.path.abspath(folder)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    try:
        return os.stat(path).st_dev, shutil.disk_usage(path)
    except OSError:
        return None
