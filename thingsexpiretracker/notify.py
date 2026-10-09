"""Ways to deliver a reminder. A notifier sends one message or raises an error."""
from __future__ import annotations

import json
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol, TextIO

from .config import Settings


class Notifier(Protocol):
    def send(self, *, to: str, subject: str, text: str, html: str) -> None: ...


class ConsoleNotifier:
    """Prints instead of sending. The default, so a fresh install never emails anyone by accident."""

    def __init__(self, out: TextIO | None = None):
        self._out = out or sys.stdout

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        print(f"--- To: {to}\n--- Subject: {subject}\n{text}\n", file=self._out)


class SmtpNotifier:
    def __init__(self, settings: Settings):
        if not settings.smtp_host or not settings.smtp_from:
            raise ValueError("Set THINGSEXPIRE_SMTP_HOST and THINGSEXPIRE_SMTP_FROM to send email.")
        self._s = settings

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        message = EmailMessage()
        message["From"] = self._s.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        if self._s.smtp_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self._s.smtp_host, self._s.smtp_port, context=context, timeout=20)
        else:
            server = smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=20)
        with server:
            if not self._s.smtp_ssl:
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls(context=context)
                    server.ehlo()
            if self._s.smtp_user:
                server.login(self._s.smtp_user, self._s.smtp_password)
            server.send_message(message)


class WebhookNotifier:
    """Posts ``{"text": ...}`` to an incoming-webhook URL (Slack, Mattermost and Teams workflows accept this shape)."""

    def __init__(self, url: str, timeout: float = 10.0):
        if not url.lower().startswith(("https://", "http://")):
            raise ValueError("THINGSEXPIRE_WEBHOOK_URL must start with https:// (or http:// for a local test server).")
        self._url = url
        self._timeout = timeout

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        payload = json.dumps({"text": f"*{subject}*\n{text}"}).encode("utf-8")
        request = urllib.request.Request(self._url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.status >= 300:
                    raise RuntimeError(f"Webhook answered HTTP {response.status}.")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Webhook answered HTTP {exc.code}.") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Webhook unreachable: {exc.reason}") from None


@dataclass
class RecordingNotifier:
    """For tests: remembers messages, and can be told to fail."""

    sent: list[dict] = field(default_factory=list)
    fail: bool = False

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        if self.fail:
            raise RuntimeError("simulated delivery failure")
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})


def get_notifier(settings: Settings) -> Notifier:
    return SmtpNotifier(settings) if settings.smtp_host else ConsoleNotifier()


def get_webhook(settings: Settings) -> Notifier | None:
    return WebhookNotifier(settings.webhook_url) if settings.webhook_url.strip() else None
