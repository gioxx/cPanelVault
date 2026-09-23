# cPanelVault

A tool to automate full backups of cPanel-based shared hosting accounts. It triggers a full backup via the cPanel UAPI, waits for it to be ready, downloads it via FTP with automatic resume support, stores it locally, and removes the remote file when done.

> **README in italiano:** [README.it.md](README.it.md) · **Website:** [cpanelvault.gioxx.org](https://cpanelvault.gioxx.org)

![cPanelVault dashboard: one host finished, one downloading with live progress and log](docs/img/dashboard.webp)

## Features

- Full backup (`fullbackup_to_homedir`) via cPanel UAPI, retried automatically on connectivity errors
- FTP download with automatic resume, socket timeouts and a free-disk-space check before each download
- Smart polling: waits until the remote file size stabilises before downloading
- Pre-existing backups left on the FTP server are downloaded first, then a fresh one is requested
- Automatic cleanup of expired local backups (configurable retention per host)
- **Multi-host**: each hosting account has its own config and independent cron schedule
- **Safe concurrency**: a per-account interprocess lock stops the CLI, the scheduler and the web UI from backing up the same cPanel account twice at the same time
- **Web UI**: dashboard with live phase, download progress (speed/ETA) and log, plus a **Backups** page listing local archives with retention, expiry and disk usage
- **CLI**: backup, clean and serve commands
- **Notifications**: Telegram, SMTP and Resend — all configurable from the JSON file
- **Docker-ready**: official multi-arch images on Docker Hub and GHCR; one `docker compose up` and you're running

## Project structure

```
backup/
  config.py     — HostConfig dataclass, JSON loader
  cpanel.py     — cPanel UAPI backup request (with retries)
  ftp.py        — connect, poll, resume download, disk-space check, remote delete
  cleaner.py    — local backup retention cleanup
  inventory.py  — local archive inventory and volume usage (Backups page)
  lock.py       — per-account interprocess lock
  runner.py     — full orchestration for one host; writes status.json
  notify.py     — Telegram / SMTP / Resend notifications
web/
  app.py        — FastAPI app: dashboard, Backups page, manual trigger, APScheduler, REST API
  templates/
    base.html     — shared layout, styles, auto-refresh
    index.html    — dashboard: host cards, live phase/progress/log
    backups.html  — Backups page: archives, retention, expiry
    _icons.html   — SVG icon macro
main.py         — CLI entry point
Dockerfile
docker-compose.yml
ftp_config_sample.json
```

## Configuration

Copy `ftp_config_sample.json` to `ftp_config.json` and fill in your credentials. This file is excluded from the repository via `.gitignore`.

```json
{
    "notifications": { ... },
    "cpanel1": {
        "host": "example.com",
        "backup_local_dest_folder": "/backups",
        "cpanel_api_token": "YOUR_TOKEN",
        "cpanel_username": "admin",
        "ftp_password": "your-ftp-password",
        "ftp_username": "backup@example.com",
        "mail_to_notify": "you@example.com",
        "time_to_wait": 60,
        "retention_days": 30,
        "schedule": "0 2 * * *"
    }
}
```

### Host fields

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | — | FTP hostname (with or without `ftp.` prefix) |
| `ftp_username` | string | — | FTP username |
| `ftp_password` | string | — | FTP password |
| `cpanel_username` | string | — | cPanel username |
| `cpanel_api_token` | string | — | cPanel API token (see below) |
| `backup_local_dest_folder` | string | — | Local root folder for backups (`/backups` in Docker) |
| `mail_to_notify` | string | — | Email address cPanel uses to notify when the backup is ready |
| `time_to_wait` | int | `60` | Seconds between size-stability checks during backup generation |
| `retention_days` | int | `30` | Local backup retention in days |
| `schedule` | string | — | Cron expression for the automatic scheduler (timezone set via `TZ`); omit for manual-only. Standard numeric weekdays are supported (0 and 7 = Sunday, 1 = Monday … 6 = Saturday) |
| `request_after_download` | bool | `true` | When a pre-existing backup is found on the FTP server and downloaded, automatically request a fresh one via the cPanel API immediately after. Prevents a gap in backup history if the scheduler runs while an old file is still sitting on the server |

The top-level key (`cpanel1`, `website`, etc.) is the name used in CLI commands and web UI URLs.

Backups are saved to `backup_local_dest_folder/<hostname>/backup-*.tar.gz`.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG_FILE` | `ftp_config.json` (`/app/ftp_config.json` in Docker) | Config file used by the web UI and scheduler. The CLI uses `--config` instead |
| `STATUS_FILE` | `status.json` (`/data/status.json` in Docker) | Last-run status of every host, read by the dashboard |
| `LOCK_DIR` | `locks/` next to `STATUS_FILE` | Lock files that prevent concurrent backups of the same account |
| `TZ` | `UTC` | Timezone for cron schedules and for the times shown in the web UI |
| `QUIET_ACCESS_LOG` | `true` | Set to `false` to log every HTTP request, including dashboard auto-refresh and healthchecks |

### Generating a cPanel API token

1. Log in to cPanel → **Manage API Tokens**
2. Create a new token with a descriptive name (e.g. `backup-script`)
3. Paste the value into `cpanel_api_token`

## Notifications

All notification channels are configured inside the `notifications` key in `ftp_config.json`. You can enable multiple channels at the same time — every enabled channel receives the message.

### Telegram

Create a bot via [@BotFather](https://t.me/BotFather) to get the token. Use [@userinfobot](https://t.me/userinfobot) or the channel/group ID (prefixed with `-100`) for `chat_id`.

```json
"notifications": {
    "telegram": {
        "enabled": true,
        "bot_token": "123456789:AABBcc...",
        "chat_id": "-100123456789"
    }
}
```

### SMTP

Works with any SMTP server. For Gmail, use an [App Password](https://myaccount.google.com/apppasswords) with `port: 587` and `use_ssl: false` (STARTTLS). For native SSL use `port: 465` and `use_ssl: true`.

```json
"smtp": {
    "enabled": true,
    "host": "smtp.gmail.com",
    "port": 587,
    "use_ssl": false,
    "username": "you@gmail.com",
    "password": "app-password",
    "from": "cPanelVault <you@gmail.com>",
    "to": "recipient@example.com"
}
```

### Resend

Cloud email alternative. Sign up at [resend.com](https://resend.com), verify your sender domain and create an API key.

```json
"resend": {
    "enabled": true,
    "api_key": "re_xxxx...",
    "from": "cPanelVault <backup@yourdomain.com>",
    "to": "recipient@example.com"
}
```

## Usage

### Docker (recommended)

Official multi-arch images (`linux/amd64`, `linux/arm64`) are published on every release:

| Registry | Image |
| --- | --- |
| Docker Hub | `gfsolone/cpanelvault` |
| GitHub Container Registry | `ghcr.io/gioxx/cpanelvault` |

Tags: `latest` (newest release), `X.Y.Z` (a specific release), `dev` (current `main`, unreleased).

```bash
cp ftp_config_sample.json ftp_config.json
# edit ftp_config.json with your credentials
docker compose up -d
```

The web UI is available at `http://localhost:8080`. The bundled `docker-compose.yml` uses `gfsolone/cpanelvault:latest`; to build from source instead, replace `image:` with `build: .`.

Backups land in the Docker volume `backups`. To save them to a fixed path on the host machine, edit `docker-compose.yml`:

```yaml
volumes:
  - ./ftp_config.json:/app/ftp_config.json:ro
  - /mnt/my-external-drive:/backups    # local path
  - data:/data
```

### Portainer

With Portainer you don't have a local project folder, so the config file must be placed on the Docker host manually before deploying the stack.

**Step 1 — create the config file on the host** (SSH into the machine running Docker):

```bash
mkdir -p /opt/cpanelvault
cp /path/to/ftp_config_sample.json /opt/cpanelvault/ftp_config.json
nano /opt/cpanelvault/ftp_config.json   # fill in your credentials
```

**Step 2 — create a new stack in Portainer** (Stacks → Add stack → Web editor) and paste:

```yaml
services:
  cpanelvault:
    image: gfsolone/cpanelvault:latest
    ports:
      - "8080:8080"
    volumes:
      - /opt/cpanelvault/ftp_config.json:/app/ftp_config.json:ro
      - cpanelvault_backups:/backups
      - cpanelvault_data:/data
    environment:
      STATUS_FILE: /data/status.json
      CONFIG_FILE: /app/ftp_config.json
      TZ: Europe/Rome   # your timezone, used by the cron schedules
    restart: unless-stopped

volumes:
  cpanelvault_backups:
  cpanelvault_data:
```

The named volumes (`cpanelvault_backups`, `cpanelvault_data`) are created automatically and visible in Portainer under **Volumes**. If you want backups on a specific host path, replace the named volume with a bind mount:

```yaml
    volumes:
      - /opt/cpanelvault/ftp_config.json:/app/ftp_config.json:ro
      - /mnt/my-external-drive:/backups
      - cpanelvault_data:/data
```

To update to a new release: **Stacks → cpanelvault → Update the stack**, with **Re-pull image** enabled. Pin a specific tag (e.g. `gfsolone/cpanelvault:2.3.1`) instead of `latest` if you prefer to upgrade manually.

### Local

```bash
pip install -r requirements.txt
cp ftp_config_sample.json ftp_config.json
# edit ftp_config.json

# Start web UI + automatic scheduler
python main.py serve

# Manual backup for a single host
python main.py backup cpanel1

# Backup all hosts sequentially
python main.py backup --all

# Preview expired backup cleanup (no deletion)
python main.py clean cpanel1 --dry-run

# Clean expired backups for all hosts
python main.py clean --all
```

### REST API

When the web UI is running the following endpoints are available:

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML dashboard |
| `GET` | `/api/status` | JSON status for all hosts |
| `GET` | `/api/hosts` | List of configured hosts |
| `GET` | `/backups` | Local archives page: volume usage, retention, expiry and removal dates |
| `GET` | `/api/backups` | Same data as JSON |
| `POST` | `/backup/<name>` | Trigger backup in background |

```bash
# Trigger from a shell script
curl -X POST http://localhost:8080/backup/cpanel1

# JSON status
curl http://localhost:8080/api/status
```

## Notes

- The remote backup file is deleted only after the local download succeeds.
- If a backup from a previous session is still on the FTP server, it is downloaded (and removed remotely) first. With `request_after_download: true` (the default) a fresh backup is requested right after, so the scheduled run still produces a current archive.
- Cron schedules and the times shown in the web UI use the timezone set by `TZ` (default UTC).
- Only one backup per cPanel account runs at a time. The lock is keyed on the FTP host (case, `ftp.` prefix and trailing dot ignored) plus `cpanel_username`, so config entries pointing at the same account are serialized too; a second run for a busy account is skipped with a warning in the log (no notification, status unchanged; the CLI exits with code 1). The CLI shares the lock with the web UI only when both use the same `LOCK_DIR`, e.g. `docker compose exec cpanelvault python main.py backup cpanel1`. In the dashboard, the entry actually running shows **Running…**, the other entries for the same account show **Busy**.
- A backup left "running" by a killed process (e.g. `docker compose down` mid-run) is marked as interrupted at the next startup.
- Transient failures are retried: the cPanel backup request after 10 and 30 minutes on connectivity errors, FTP downloads every 10 seconds (resuming from where they stopped), remote deletes up to 5 times. A download is aborted up front if the backup volume doesn't have enough free space.
- All logs go to stdout; with Docker use `docker compose logs -f`.
- While a backup runs, the dashboard shows the current phase (checking FTP, waiting for cPanel, stability check, downloading, retention), download progress and a collapsible live log; after the run the panel keeps the last run's log. The page refreshes every 5s while a backup is running, 30s otherwise.
- The **Backups** page lists the archives on the backup volume per host, with size, cPanel timestamp, download date, expiry (download date + `retention_days`) and the scheduled run that will remove them. Cleanup only happens at the end of a successful backup, so an expired archive stays until the next run.
- Successful `GET /`, `GET /api/status` and `GET /static/*` requests (dashboard auto-refresh, Docker healthcheck) are not written to the access log. Set `QUIET_ACCESS_LOG=false` to log every request.
