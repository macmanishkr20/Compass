"""Notification delivery for routine runs — email (SMTP) + a push feed.

Push (laptop/desktop) is delivered by the frontend via the Web Notifications API:
the server records finished runs and the browser polls /v1/routine-runs/recent,
firing a native notification for any routine that has push enabled. Email is sent
here over SMTP when it's configured.

SMTP config (env): COMPASS_SMTP_HOST, COMPASS_SMTP_PORT (default 587),
COMPASS_SMTP_USER, COMPASS_SMTP_PASSWORD, COMPASS_SMTP_FROM (default = USER),
COMPASS_SMTP_TLS (default "1"). Recipient: COMPASS_NOTIFY_EMAIL.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("compass.notify")


def email_configured() -> bool:
    return bool(os.environ.get("COMPASS_SMTP_HOST") and os.environ.get("COMPASS_NOTIFY_EMAIL"))


def send_email(subject: str, body: str, *, to: str | None = None) -> bool:
    """Send a plain-text email via SMTP. Returns True on success, False if SMTP
    isn't configured or the send failed (logged, never raised)."""
    host = os.environ.get("COMPASS_SMTP_HOST")
    recipient = to or os.environ.get("COMPASS_NOTIFY_EMAIL", "")
    if not host or not recipient:
        logger.info("email skipped — SMTP not configured (set COMPASS_SMTP_* + COMPASS_NOTIFY_EMAIL)")
        return False

    port = int(os.environ.get("COMPASS_SMTP_PORT", "587"))
    user = os.environ.get("COMPASS_SMTP_USER", "")
    password = os.environ.get("COMPASS_SMTP_PASSWORD", "")
    sender = os.environ.get("COMPASS_SMTP_FROM") or user or "compass@localhost"
    use_tls = os.environ.get("COMPASS_SMTP_TLS", "1") not in ("0", "false", "False")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if use_tls:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("routine email sent to %s: %s", recipient, subject)
        return True
    except Exception as err:  # noqa: BLE001 — notifications must never break a run
        logger.warning("routine email failed: %s", err)
        return False
