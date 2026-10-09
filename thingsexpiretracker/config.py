"""Settings read from environment variables, so secrets stay out of the code and the database."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: str = "thingsexpiretracker.db"
    app_name: str = "ThingsExpireTracker"
    api_token: str = ""  # required when the server listens on anything but localhost
    default_recipient: str = ""  # gets reminders for items that have no owner email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    currency: str = "USD"  # shown next to costs; amounts are not converted
    escalate_to: str = ""  # also told when an item has been overdue for more than a week
    webhook_url: str = ""  # Slack/Teams-style incoming webhook; also the channel for items with no owner email
    public_url: str = ""  # e.g. https://expiry.example.com; enables one-click "I'm on it" links in emails
    secret: str = ""  # signs those links (falls back to the API token)
    attachments_dir: str = ""  # default: <database file>.files
    ai_key: str = ""  # Anthropic API key; when set, invoices are sent to Claude to be read
    ai_model: str = "claude-sonnet-5-5"
    ai_url: str = "https://api.anthropic.com/v1/messages"
    smtp_ssl: bool = False  # True = implicit TLS (port 465); False = STARTTLS when the server offers it

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        port_text = env.get("THINGSEXPIRE_SMTP_PORT", "").strip()
        return cls(
            db_path=env.get("THINGSEXPIRE_DB", "thingsexpiretracker.db"),
            app_name=env.get("THINGSEXPIRE_APP_NAME", "ThingsExpireTracker"),
            api_token=env.get("THINGSEXPIRE_TOKEN", ""),
            default_recipient=env.get("THINGSEXPIRE_DEFAULT_RECIPIENT", ""),
            smtp_host=env.get("THINGSEXPIRE_SMTP_HOST", ""),
            smtp_port=int(port_text) if port_text.isdigit() else 587,
            smtp_user=env.get("THINGSEXPIRE_SMTP_USER", ""),
            smtp_password=env.get("THINGSEXPIRE_SMTP_PASSWORD", ""),
            smtp_from=env.get("THINGSEXPIRE_SMTP_FROM", ""),
            currency=(env.get("THINGSEXPIRE_CURRENCY", "USD").strip() or "USD")[:8],
            escalate_to=env.get("THINGSEXPIRE_ESCALATE_TO", ""),
            webhook_url=env.get("THINGSEXPIRE_WEBHOOK_URL", ""),
            public_url=env.get("THINGSEXPIRE_PUBLIC_URL", "").rstrip("/"),
            secret=env.get("THINGSEXPIRE_SECRET", ""),
            attachments_dir=env.get("THINGSEXPIRE_FILES_DIR", ""),
            ai_key=env.get("THINGSEXPIRE_ANTHROPIC_KEY", ""),
            ai_model=env.get("THINGSEXPIRE_AI_MODEL", "claude-sonnet-5-5") or "claude-sonnet-5-5",
            ai_url=env.get("THINGSEXPIRE_AI_URL", "https://api.anthropic.com/v1/messages") or "https://api.anthropic.com/v1/messages",
            smtp_ssl=_flag(env.get("THINGSEXPIRE_SMTP_SSL"), False),
        )
