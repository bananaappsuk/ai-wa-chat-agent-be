"""Minimal email delivery abstraction (log / SMTP / SendGrid)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def send_email(
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
) -> bool:
    """Send email. Returns True on accepted send/log. Never raises secrets."""
    provider = (settings.EMAIL_PROVIDER or "log").strip().lower()
    if not settings.PASSWORD_RESET_EMAIL_ENABLED and provider == "log":
        logger.info("email skipped (disabled) to=%s subject=%s", _mask_email(to), subject[:80])
        return True

    if provider in ("", "log", "none", "dev"):
        logger.info(
            "email[dev] to=%s subject=%s body_len=%s",
            _mask_email(to),
            subject[:80],
            len(text_body or ""),
        )
        return True

    if provider == "sendgrid":
        return _send_sendgrid(to=to, subject=subject, text_body=text_body, html_body=html_body)
    if provider == "smtp":
        return _send_smtp(to=to, subject=subject, text_body=text_body, html_body=html_body)
    logger.warning("Unknown EMAIL_PROVIDER=%s — logged only", provider)
    logger.info("email[fallback] to=%s subject=%s", _mask_email(to), subject[:80])
    return True


def _mask_email(email: str) -> str:
    raw = (email or "").strip()
    if "@" not in raw:
        return "***"
    local, _, domain = raw.partition("@")
    return f"{local[:1]}***@{domain}"


def _send_smtp(*, to: str, subject: str, text_body: str, html_body: Optional[str]) -> bool:
    host = (settings.SMTP_HOST or "").strip()
    if not host:
        logger.warning("SMTP_HOST not configured")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(host, int(settings.SMTP_PORT), timeout=20) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            user = (settings.SMTP_USERNAME or "").strip()
            if user:
                server.login(user, settings.SMTP_PASSWORD or "")
            server.sendmail(settings.EMAIL_FROM, [to], msg.as_string())
        return True
    except Exception:
        logger.exception("SMTP send failed")
        return False


def _send_sendgrid(*, to: str, subject: str, text_body: str, html_body: Optional[str]) -> bool:
    key = (settings.SENDGRID_API_KEY or "").strip()
    if not key:
        logger.warning("SENDGRID_API_KEY not configured")
        return False
    try:
        import json
        import urllib.request

        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": settings.EMAIL_FROM},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text_body}],
        }
        if html_body:
            payload["content"].append({"type": "text/html", "value": html_body})
        req = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= getattr(resp, "status", 202) < 300
    except Exception:
        logger.exception("SendGrid send failed")
        return False


def build_password_reset_email(*, reset_url: str, user_name: str = "") -> tuple[str, str, str]:
    subject = "Reset your password"
    greet = f"Hi {user_name}," if user_name else "Hi,"
    text = (
        f"{greet}\n\n"
        "We received a request to reset your password. "
        f"Open this link to choose a new password (expires soon):\n\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email.\n"
    )
    # Safe HTML — URL is our own constructed link
    safe_url = reset_url.replace('"', "%22")
    html = (
        f"<p>{greet}</p>"
        "<p>We received a request to reset your password.</p>"
        f'<p><a href="{safe_url}">Reset your password</a></p>'
        "<p>If you did not request this, you can ignore this email.</p>"
    )
    return subject, text, html
