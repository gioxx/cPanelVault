import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backup import fmt_size
from backup.config import HostConfig, load_config, load_notifications
from backup.inventory import list_backups, volume_usage
from backup.runner import load_status, reconcile_stale_running, run_backup
from main import __version__

log = logging.getLogger(__name__)

_CRON_DOW = {"0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat", "7": "sun"}


def _normalize_cron(expr: str) -> str:
    """Convert numeric day-of-week (crontab: 0=Sun) to named days for APScheduler."""
    parts = expr.split()
    if len(parts) != 5:
        return expr
    if parts[4] in _CRON_DOW:
        parts[4] = _CRON_DOW[parts[4]]
    return " ".join(parts)

CONFIG_PATH = os.environ.get("CONFIG_FILE", "ftp_config.json")
_HERE = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(_HERE, "templates")
STATIC_DIR = os.path.join(_HERE, "static")

# Successful GETs on these paths come from dashboard auto-refresh and the Docker
# healthcheck: they flood the container log without adding information.
_QUIET_ACCESS_PATHS = {"/", "/api/status", "/favicon.ico"}
QUIET_ACCESS_LOG = os.environ.get("QUIET_ACCESS_LOG", "true").lower() not in ("0", "false", "no")

PHASE_LABELS = {
    "checking": "Checking FTP",
    "existing_wait": "Pre-existing backup: stability check",
    "existing_download": "Pre-existing backup: downloading",
    "requesting": "Requesting backup",
    "waiting": "Waiting for cPanel",
    "downloading": "Downloading",
    "cleaning": "Applying retention",
}


class _QuietAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, path, _, status_code = args
        path = str(path).split("?", 1)[0]
        quiet = path in _QUIET_ACCESS_PATHS or path.startswith("/static/")
        return not (method == "GET" and quiet and int(status_code) < 400)


def _quiet_logs() -> None:
    # APScheduler logs every job add/run at INFO; our own "Scheduled ..." line covers it.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    if QUIET_ACCESS_LOG:
        logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())


templates = Jinja2Templates(directory=TEMPLATES_DIR)
# Changes on every process start. The dashboard's in-place refresh compares
# it and does a full reload after a restart/upgrade, so an open tab never
# keeps stale page chrome (nav, styles, scripts) around new content.
templates.env.globals["boot_id"] = uuid.uuid4().hex
_running: set[str] = set()
_running_lock = threading.Lock()
SCHEDULER_TZ = os.environ.get("TZ", "UTC")
_scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)


def _claim(name: str) -> bool:
    """Mark `name` as running; False if a run for it is already in progress."""
    with _running_lock:
        if name in _running:
            return False
        _running.add(name)
        return True


def _run_in_thread(cfg: HostConfig, claimed: bool = False) -> None:
    if not claimed and not _claim(cfg.name):
        log.warning("[%s] Backup already running, skipping.", cfg.name)
        return
    try:
        notifications = load_notifications(CONFIG_PATH)
        run_backup(cfg, notifications)
    finally:
        _running.discard(cfg.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Applied here rather than at import time: uvicorn configures its loggers
    # after importing the app, so this is the first point our changes stick.
    _quiet_logs()
    reconcile_stale_running()
    cfg = load_config(CONFIG_PATH)
    for name, host_cfg in cfg.items():
        if host_cfg.schedule:
            _scheduler.add_job(
                _run_in_thread,
                CronTrigger.from_crontab(_normalize_cron(host_cfg.schedule), timezone=SCHEDULER_TZ),
                args=[host_cfg],
                id=name,
                replace_existing=True,
            )
            log.info("Scheduled %s: %s (%s)", name, host_cfg.schedule, SCHEDULER_TZ)
    _scheduler.start()
    log.info("Scheduler started (%d scheduled host(s)).", len(_scheduler.get_jobs()))
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="cPanelVault", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



def _fmt_duration(s: int | None) -> str:
    if s is None:
        return "—"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def _fmt_ended(iso: str | None) -> str:
    """Stored timestamps are UTC ISO strings; show them in the scheduler's timezone."""
    if not iso:
        return "—"
    try:
        return _fmt_dt(datetime.fromisoformat(iso).astimezone(_scheduler.timezone))
    except ValueError:
        return iso[:16].replace("T", " ")


def _fmt_rel(delta: timedelta) -> str:
    """Compact relative time: "in 3d 4h", "2h 5m ago"."""
    secs = int(delta.total_seconds())
    future = secs >= 0
    secs = abs(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        text = f"{d}d {h}h" if h else f"{d}d"
    elif h:
        text = f"{h}h {m}m" if m else f"{h}h"
    else:
        text = f"{max(m, 1)}m"
    return f"in {text}" if future else f"{text} ago"


def _rate_text(done: int | None, total: int | None, speed: float | None) -> str | None:
    """"14.6 MiB/s · ~5m 40s left", from the same sample as the progress bar."""
    if done is None or not total or not speed:
        return None
    eta = int(max(total - done, 0) / speed)
    return f"{fmt_size(int(speed))}/s · ~{_fmt_duration(eta)} left"


def _next_run(name: str) -> str:
    job = _scheduler.get_job(name)
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%d %H:%M")
    return "—"


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = load_config(CONFIG_PATH)
    status = load_status()
    hosts = []
    for name, host_cfg in cfg.items():
        s = status.get(name, {})
        progress = s.get("progress") or {}
        done, total = progress.get("done"), progress.get("total")
        hosts.append({
            "name": name,
            "host": host_cfg.cpanel_host,
            "schedule": host_cfg.schedule or "Manual",
            "retention_days": host_cfg.retention_days,
            "tz": SCHEDULER_TZ,
            "next_run": _next_run(name),
            "status": s.get("status", "never"),
            "file": s.get("file", "—"),
            "size": fmt_size(s.get("size_bytes")),
            "ended": _fmt_ended(s.get("ended")),
            "duration": _fmt_duration(s.get("duration_seconds")),
            "error": s.get("error"),
            "running": name in _running,
            "phase": PHASE_LABELS.get(s.get("phase"), s.get("phase")),
            "last_message": s.get("last_message"),
            "progress_pct": done * 100 // total if done is not None and total else None,
            "progress_text": f"{fmt_size(done)} / {fmt_size(total)}" if done is not None and total else None,
            "rate_text": _rate_text(done, total, progress.get("speed")),
            "log_lines": s.get("log_lines") or [],
            "log_total": s.get("log_total"),
        })
    any_running = any(h["running"] for h in hosts)
    return templates.TemplateResponse(request, "index.html", {
        "hosts": hosts,
        "page": "dashboard",
        "version": __version__,
        "refresh_seconds": 5 if any_running else 30,
    })


# Backups expiring within this window are flagged in the UI.
_EXPIRING_SOON = timedelta(days=3)


def _copies_kept(trigger, now: datetime, retention: timedelta) -> int:
    """Archives kept right after a run in steady state: that run's own plus
    every earlier run still younger than the retention period, i.e. the
    number of runs in [next_run, next_run + retention)."""
    first = trigger.get_next_fire_time(None, now)
    if first is None:
        return 0
    count, t = 0, first
    while t is not None and t < first + retention and count < 1000:
        count += 1
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return count


def _backup_inventory() -> dict:
    cfg = load_config(CONFIG_PATH)
    status = load_status()
    tz = _scheduler.timezone
    now = datetime.now(tz)
    hosts, volumes = [], {}

    for name, host_cfg in cfg.items():
        s = status.get(name, {})
        job = _scheduler.get_job(name)
        trigger = job.trigger if job else None
        retention = timedelta(days=host_cfg.retention_days)
        in_progress = s.get("current_file") if name in _running else None

        files = []
        for b in list_backups(host_cfg.destination_folder):
            downloaded = datetime.fromtimestamp(b.mtime, tz)
            expires = downloaded + retention
            # Cleanup only runs at the end of a successful backup, so an
            # expired archive goes away at the first run after it expires.
            removal = trigger.get_next_fire_time(None, max(expires, now)) if trigger else None
            if b.name == in_progress:
                state = "downloading"
            elif expires <= now:
                state = "expired"
            elif expires - now <= _EXPIRING_SOON:
                state = "soon"
            else:
                state = "ok"
            files.append({
                "name": b.name,
                "path": b.path,
                "account": b.account,
                "size_bytes": b.size_bytes,
                "size": fmt_size(b.size_bytes),
                "created": b.created.strftime("%Y-%m-%d %H:%M") if b.created else "—",
                "downloaded": _fmt_dt(downloaded),
                "downloaded_iso": downloaded.isoformat(),
                "age": _fmt_rel(downloaded - now),
                "expires": _fmt_dt(expires),
                "expires_iso": expires.isoformat(),
                "expires_rel": _fmt_rel(expires - now),
                "removal": _fmt_dt(removal) if removal else ("next run" if not trigger else "—"),
                "removal_iso": removal.isoformat() if removal else None,
                "state": state,
            })

        total_bytes = sum(f["size_bytes"] for f in files)
        vu = volume_usage(host_cfg.destination_folder)
        usage = vu[1] if vu else None
        # The next archive will be about as large as the latest one.
        latest = next((f for f in files if f["state"] != "downloading"), None)
        low_space = bool(usage and latest and usage.free < latest["size_bytes"])
        if vu:
            dev, usage = vu
            vol = volumes.setdefault(dev, {
                "path": host_cfg.backup_local_dest_folder,
                "total": fmt_size(usage.total),
                "used": fmt_size(usage.used),
                "free": fmt_size(usage.free),
                "free_bytes": usage.free,
                "used_pct": usage.used * 100 // usage.total if usage.total else 0,
                "backups_bytes": 0,
                "hosts": [],
            })
            vol["backups_bytes"] += total_bytes
            vol["hosts"].append(name)

        pending = [f for f in files if f["state"] in ("expired", "soon")]
        next_cleanup = min((f["removal_iso"] for f in pending if f["removal_iso"]), default=None)
        hosts.append({
            "name": name,
            "host": host_cfg.cpanel_host,
            "folder": host_cfg.destination_folder,
            "retention_days": host_cfg.retention_days,
            "schedule": host_cfg.schedule or "Manual",
            "next_run": _fmt_dt(job.next_run_time) if job and job.next_run_time else "—",
            "copies_kept": _copies_kept(trigger, now, retention) if trigger else None,
            "count": len(files),
            "total": fmt_size(total_bytes),
            "total_bytes": total_bytes,
            "low_space": low_space,
            "next_cleanup": _fmt_dt(datetime.fromisoformat(next_cleanup)) if next_cleanup else None,
            "pending_cleanup": len(pending),
            "files": files,
        })

    for vol in volumes.values():
        vol["backups"] = fmt_size(vol.pop("backups_bytes"))
    return {"tz": SCHEDULER_TZ, "now": _fmt_dt(now), "hosts": hosts, "volumes": list(volumes.values())}


@app.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    return templates.TemplateResponse(request, "backups.html", {
        **_backup_inventory(),
        "page": "backups",
        "version": __version__,
    })


@app.get("/api/backups")
async def api_backups():
    return _backup_inventory()


@app.post("/backup/{name}")
async def trigger_backup(name: str):
    cfg = load_config(CONFIG_PATH)
    if name not in cfg:
        return {"error": "Host not found"}
    # Claim before redirecting, so the dashboard loaded right after the
    # redirect already shows the run (and refreshes at the fast interval).
    if _claim(name):
        threading.Thread(target=_run_in_thread, args=[cfg[name], True], daemon=True).start()
    else:
        log.warning("[%s] Backup already running, skipping.", name)
    # ?log=<name> tells the page to open that host's log panel.
    return RedirectResponse(f"/?log={quote(name)}", status_code=303)


@app.get("/api/status")
async def api_status():
    return load_status()


@app.get("/api/hosts")
async def api_hosts():
    cfg = load_config(CONFIG_PATH)
    return [
        {
            "name": name,
            "host": h.cpanel_host,
            "schedule": h.schedule,
            "retention_days": h.retention_days,
        }
        for name, h in cfg.items()
    ]
