import logging
import os
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backup.config import HostConfig, load_config, load_notifications
from backup.runner import load_status, run_backup

log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_FILE", "ftp_config.json")
_HERE = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(_HERE, "templates")
STATIC_DIR = os.path.join(_HERE, "static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)
_running: set[str] = set()
SCHEDULER_TZ = os.environ.get("TZ", "UTC")
_scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)


def _run_in_thread(cfg: HostConfig) -> None:
    if cfg.name in _running:
        log.warning("[%s] Backup already running, skipping.", cfg.name)
        return
    _running.add(cfg.name)
    try:
        notifications = load_notifications(CONFIG_PATH)
        run_backup(cfg, notifications)
    finally:
        _running.discard(cfg.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(CONFIG_PATH)
    for name, host_cfg in cfg.items():
        if host_cfg.schedule:
            _scheduler.add_job(
                _run_in_thread,
                CronTrigger.from_crontab(host_cfg.schedule, timezone=SCHEDULER_TZ),
                args=[host_cfg],
                id=name,
                replace_existing=True,
            )
            log.info("Scheduled %s: %s (%s)", name, host_cfg.schedule, SCHEDULER_TZ)
    _scheduler.start()
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="Hosting Backup Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


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
        hosts.append({
            "name": name,
            "host": host_cfg.cpanel_host,
            "schedule": host_cfg.schedule or "Manual",
            "retention_days": host_cfg.retention_days,
            "tz": SCHEDULER_TZ,
            "next_run": _next_run(name),
            "status": s.get("status", "never"),
            "file": s.get("file", "—"),
            "size": _fmt_size(s.get("size_bytes")),
            "ended": (s.get("ended") or "—")[:19].replace("T", " "),
            "duration": _fmt_duration(s.get("duration_seconds")),
            "error": s.get("error"),
            "running": name in _running,
        })
    return templates.TemplateResponse(request, "index.html", {"hosts": hosts})


@app.post("/backup/{name}")
async def trigger_backup(name: str):
    cfg = load_config(CONFIG_PATH)
    if name not in cfg:
        return {"error": "Host not found"}
    t = threading.Thread(target=_run_in_thread, args=[cfg[name]], daemon=True)
    t.start()
    return RedirectResponse("/", status_code=303)


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
