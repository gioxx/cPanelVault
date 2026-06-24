import json
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class HostConfig:
    name: str
    host: str
    ftp_username: str
    ftp_password: str
    cpanel_username: str
    cpanel_api_token: str
    backup_local_dest_folder: str
    mail_to_notify: str
    time_to_wait: int = 60
    retention_days: int = 30
    schedule: Optional[str] = None
    request_after_download: bool = True

    @property
    def cpanel_host(self) -> str:
        return self.host.removeprefix("ftp.")

    @property
    def destination_folder(self) -> str:
        return os.path.join(self.backup_local_dest_folder, self.cpanel_host)


def load_config(path: str) -> dict[str, HostConfig]:
    with open(path) as f:
        raw = json.load(f)
    return {
        name: HostConfig(name=name, **data)
        for name, data in raw.items()
        if name != "notifications"
    }


def load_notifications(path: str) -> dict:
    with open(path) as f:
        raw = json.load(f)
    return raw.get("notifications", {})
