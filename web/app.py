import logging
import os
import threading
from contextlib import asynccontextmanager
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backup import fmt_size
from backup.config import HostConfig, load_config, load_notifications
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
            "ended": (s.get("ended") or "—")[:16].replace("T", " "),
            "duration": _fmt_duration(s.get("duration_seconds")),
            "error": s.get("error"),
            "running": name in _running,
            "phase": PHASE_LABELS.get(s.get("phase"), s.get("phase")),
            "last_message": s.get("last_message"),
            "progress_pct": done * 100 // total if done is not None and total else None,
            "progress_text": f"{fmt_size(done)} / {fmt_size(total)}" if done is not None and total else None,
            "log_lines": s.get("log_lines") or [],
            "log_total": s.get("log_total"),
        })
    any_running = any(h["running"] for h in hosts)
    return templates.TemplateResponse(request, "index.html", {
        "hosts": hosts,
        "version": __version__,
        "refresh_seconds": 5 if any_running else 30,
    })


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
