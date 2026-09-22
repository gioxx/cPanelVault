import logging
import time

import requests

log = logging.getLogger(__name__)

# Wait between retries on connectivity errors: 10 min, then 30 min.
_RETRY_WAIT_SECONDS = [600, 1800]


def request_backup(cpanel_host: str, username: str, token: str, mail: str) -> bool:
    url = f"https://{cpanel_host}:2083/execute/Backup/fullbackup_to_homedir"
    headers = {"Authorization": f"cpanel {username}:{token}"}

    attempts = len(_RETRY_WAIT_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=headers, params={"email": mail}, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == attempts:
                log.error("Backup request failed after %d attempt(s): %s", attempt, e)
                return False
            wait = _RETRY_WAIT_SECONDS[attempt - 1]
            log.warning(
                "Backup request connectivity error (attempt %d/%d): %s — retrying in %d minutes",
                attempt, attempts, e, wait // 60,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            data = resp.json()
            pid = (data.get("data") or {}).get("pid")
            log.info("Backup request accepted by cPanel (pid %s).", pid or "n/a")
            if data.get("errors") or data.get("warnings"):
                log.warning("cPanel response: %s", data)
            else:
                log.debug("cPanel response: %s", data)
            return True
        log.error("Backup request failed: HTTP %s", resp.status_code)
        return False

    return False
