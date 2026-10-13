# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only

"""
adapters/mailer.py
==================
Two notifiers behind one interface: a file sink and real SMTP.

    LONGSUM_MAIL_SINK = file | smtp | none      (default: file)
    LONGSUM_MAIL_DIR  = where .eml files land   (default: <project>/mail_out)
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM

THE DEFAULT IS `file`, DELIBERATELY. This runs against a de-identified research
cohort under a data use agreement, and a demo that mails a real person is a
defect with consequences. Writing a .eml to disk exercises the entire path -
template, recipient resolution, idempotency, outbox state - and transmits
nothing. Switching to `smtp` is one environment variable when a live send is
actually wanted.

The files are also the demo artefact: you can open them, read exactly what a
physician would have received, and put them in front of a reviewer.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

try:
    from support.adapters.base import Delivery
except ImportError:  # pragma: no cover
    from adapters.base import Delivery

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _mail_dir() -> Path:
    d = os.getenv("LONGSUM_MAIL_DIR")
    if d:
        return Path(d)
    try:
        import config
        return Path(config.PROJECT_ROOT) / "mail_out"
    except Exception:
        return Path.cwd() / "mail_out"


def _build(to_email: str, to_name: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM", "longsum-feedback@example.invalid")
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg["Subject"] = subject
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    # Marks the message for any downstream mail rule or DLP scanner. Cheap, and
    # it means the distribution restriction is machine-readable as well as
    # printed in the body.
    msg["X-Longsum-Sensitivity"] = "patient-information; internal-only"
    msg.set_content(body)
    return msg


class FileSink:
    """Writes .eml files instead of sending. The default, and the demo artefact."""

    name = "file"

    def send(self, to_email: str, to_name: str, subject: str, body: str) -> Delivery:
        try:
            d = _mail_dir()
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            fname = f"{stamp}_{_SAFE.sub('_', subject)[:70]}.eml"
            path = d / fname
            path.write_bytes(_build(to_email, to_name, subject, body).as_bytes())
            return Delivery.sent(external_ref=str(path), detail=f"written to {path}")
        except Exception as e:
            return Delivery.fail(f"file sink write failed: {e}")


class SmtpMailer:
    """Real send. Only reached when LONGSUM_MAIL_SINK=smtp."""

    name = "smtp"

    def send(self, to_email: str, to_name: str, subject: str, body: str) -> Delivery:
        import smtplib
        host = os.getenv("SMTP_HOST")
        if not host:
            return Delivery.fail("LONGSUM_MAIL_SINK=smtp but SMTP_HOST is not set")
        try:
            msg = _build(to_email, to_name, subject, body)
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 25)), timeout=20) as s:
                if os.getenv("SMTP_USER"):
                    s.starttls()
                    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
                s.send_message(msg)
            return Delivery.sent(detail=f"sent to {to_email} via {host}")
        except Exception as e:
            return Delivery.fail(f"smtp send failed: {e}")


class NullMailer:
    """Sends nothing and says so. For runs where mail is simply not wanted."""

    name = "none"

    def send(self, to_email: str, to_name: str, subject: str, body: str) -> Delivery:
        return Delivery.skip("mail disabled (LONGSUM_MAIL_SINK=none)")


def get_mailer():
    sink = (os.getenv("LONGSUM_MAIL_SINK") or "file").strip().lower()
    return {"file": FileSink, "smtp": SmtpMailer, "none": NullMailer}.get(sink, FileSink)()
