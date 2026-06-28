import logging

import requests

log = logging.getLogger(__name__)


def request_backup(cpanel_host: str, username: str, token: str, mail: str) -> bool:
    url = f"https://{cpanel_host}:2083/execute/Backup/fullbackup_to_homedir"
    headers = {"Authorization": f"cpanel {username}:{token}"}
    resp = requests.get(url, headers=headers, params={"email": mail}, timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        log.info("Backup request accepted: %s", data)
        return True
    log.error("Backup request failed: HTTP %s", resp.status_code)
    return False
