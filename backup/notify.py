import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

log = logging.getLogger(__name__)


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _build_message(result: dict) -> tuple[str, str]:
    name = result.get("name", "unknown")
    status = result.get("status", "unknown")
    if status == "success":
        subject = f"[OK] Backup {name} completed"
        body = (
            f"Backup completed successfully.\n\n"
            f"Host:     {name}\n"
            f"File:     {result.get('file', '—')}\n"
            f"Size:     {_fmt_size(result.get('size_bytes'))}\n"
            f"Duration: {result.get('duration_seconds', 0)}s\n"
            f"Finished: {(result.get('ended') or '')[:19].replace('T', ' ')} UTC\n"
            f"Cleaned:  {result.get('cleaned', 0)} expired file(s)"
        )
    else:
        subject = f"[ERROR] Backup {name} failed"
        body = (
            f"Backup failed.\n\n"
            f"Host:     {name}\n"
            f"Error:    {result.get('error', '—')}\n"
            f"Duration: {result.get('duration_seconds', 0)}s\n"
            f"Started:  {(result.get('started') or '')[:19].replace('T', ' ')} UTC"
        )
    return subject, body


def _send_telegram(token: str, chat_id: str, subject: str, body: str) -> None:
    text = f"*{subject}*\n\n```\n{body}\n```"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.ok:
            log.info("Telegram notification sent.")
        else:
            log.warning("Telegram notification failed: %s", resp.text)
    except Exception as e:
        log.warning("Telegram notification error: %s", e)


def _send_smtp(cfg: dict, subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.attach(MIMEText(body, "plain"))

    ctx = ssl.create_default_context()
    try:
        if cfg.get("use_ssl", False):
            with smtplib.SMTP_SSL(cfg["host"], cfg.get("port", 465), context=ctx) as s:
                s.login(cfg["username"], cfg["password"])
                s.sendmail(cfg["from"], cfg["to"], msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg.get("port", 587)) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(cfg["username"], cfg["password"])
                s.sendmail(cfg["from"], cfg["to"], msg.as_string())
        log.info("SMTP notification sent to %s.", cfg["to"])
    except Exception as e:
        log.warning("SMTP notification failed: %s", e)


def _send_resend(cfg: dict, subject: str, body: str) -> None:
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={"from": cfg["from"], "to": [cfg["to"]], "subject": subject, "text": body},
            timeout=10,
        )
        if resp.ok:
            log.info("Resend notification sent to %s.", cfg["to"])
        else:
            log.warning("Resend notification failed: %s", resp.text)
    except Exception as e:
        log.warning("Resend notification error: %s", e)


def notify(notifications: dict, result: dict) -> None:
    """Dispatch backup result to all enabled notification channels."""
    if not notifications:
        return

    subject, body = _build_message(result)

    tg = notifications.get("telegram", {})
    if tg.get("enabled"):
        _send_telegram(tg["bot_token"], tg["chat_id"], subject, body)

    smtp = notifications.get("smtp", {})
    if smtp.get("enabled"):
        _send_smtp(smtp, subject, body)

    resend_cfg = notifications.get("resend", {})
    if resend_cfg.get("enabled"):
        _send_resend(resend_cfg, subject, body)
