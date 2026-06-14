import logging
import os
import time

log = logging.getLogger(__name__)


def clean_old_backups(folder: str, retention_days: int, dry_run: bool = False) -> list[str]:
    if not os.path.isdir(folder):
        return []
    cutoff = time.time() - retention_days * 86400
    removed: list[str] = []
    for root, _, files in os.walk(folder):
        for name in files:
            if not name.endswith(".tar.gz"):
                continue
            path = os.path.join(root, name)
            if os.path.getmtime(path) < cutoff:
                removed.append(path)
                if not dry_run:
                    os.remove(path)
                    log.info("Deleted old backup: %s", path)
                else:
                    log.info("[dry-run] Would delete: %s", path)
    return removed
