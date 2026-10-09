"""Finds the reminders that are due and sends each person a single message."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from html import escape
from typing import Any, Callable, Optional

from . import rules, store
from .notify import Notifier

WEBHOOK_RECIPIENT = "webhook"  # what the log records for items delivered only to the team channel
ESCALATION_STAGE = -2  # overdue for more than a week


@dataclass
class Pending:
    item: dict[str, Any]
    stage: int
    recipient: str  # "" means: no person to email, deliver to the webhook channel only
    escalation: bool = False

    @property
    def log_recipient(self) -> str:
        return self.recipient or WEBHOOK_RECIPIENT


@dataclass
class RunReport:
    sent: int = 0  # emails delivered
    items_notified: int = 0
    failed: int = 0
    webhook_sent: int = 0
    webhook_failed: int = 0
    no_recipient: list[str] = field(default_factory=list)  # item names with nobody to tell
    planned: list[Pending] = field(default_factory=list)  # filled for dry runs


def due_reminders(
    conn: sqlite3.Connection,
    today: date,
    default_recipient: str = "",
    *,
    escalate_to: str = "",
    webhook: bool = False,
) -> tuple[list[Pending], list[str]]:
    """Returns (reminders that should go out, names of due items that have nobody to tell).

    Each (item, expiry date, stage, recipient) is delivered once, so renewing an item or changing its owner
    behaves naturally: a new expiry date or a new recipient is a new reminder."""
    pending: list[Pending] = []
    orphans: list[str] = []
    for item in store.list_items(conn, today=today):
        stage = rules.reminder_stage(item["days_left"], item["lead_days"])
        if stage is None:
            continue
        owner = item["owner_email"] or default_recipient
        snoozed = bool(item["snoozed_until"] and today.isoformat() <= item["snoozed_until"])
        in_progress = bool(item["workflow"] and item["days_left"] > 0)
        if snoozed or in_progress:
            pass  # someone has said they are on it; the escalation check below is deliberately not paused
        elif owner:
            if not store.reminder_already_sent(conn, item["id"], item["expires_on"], stage, owner):
                pending.append(Pending(item, stage, owner))
        elif webhook:
            if not store.reminder_already_sent(conn, item["id"], item["expires_on"], stage, WEBHOOK_RECIPIENT):
                pending.append(Pending(item, stage, ""))
        else:
            orphans.append(item["name"])
        if (
            escalate_to
            and stage <= ESCALATION_STAGE
            and escalate_to.lower() != owner.lower()
            and not store.reminder_already_sent(conn, item["id"], item["expires_on"], stage, escalate_to)
        ):
            pending.append(Pending(item, stage, escalate_to, escalation=True))
    return pending, orphans


def _when(item: dict[str, Any]) -> str:
    left = item["days_left"]
    if left < 0:
        return f"expired {-left} day{'s' if left != -1 else ''} ago"
    if left == 0:
        return "expires today"
    return f"expires in {left} day{'s' if left != 1 else ''}"


def build_message(
    pendings: list[Pending], app_name: str = "ThingsExpireTracker", *, escalation: bool = False, currency: str = "",
    links: Optional[dict[int, str]] = None,
) -> tuple[str, str, str]:
    """(subject, text, html) for one recipient. Most urgent items come first."""
    ordered = sorted(pendings, key=lambda p: (p.item["days_left"], p.item["name"].lower()))
    expired = sum(1 for p in ordered if p.item["days_left"] < 0)
    count = len(ordered)
    plural = count != 1
    if escalation:
        subject = f"{app_name} escalation: {count} item{'s' if plural else ''} overdue for more than a week"
    else:
        subject = f"{app_name}: {count} item{'s' if plural else ''} need{'' if plural else 's'} attention"
        if expired:
            subject += f" ({expired} expired)"

    intro = "These items are still overdue and have not been renewed:" if escalation else "These items need attention:"
    footer = "Renew them, then record the new date so the reminders stop."
    lines = [intro, ""]
    rows = []
    cell = "padding:6px 10px;border-top:1px solid #e5e7eb"
    for p in ordered:
        item = p.item
        extras = [x for x in (f"ref {item['reference']}" if item["reference"] else "",
                              item["vendor"],
                              f"{item['cost']} {currency}".strip() if item["cost"] else "",
                              f"owner {item['owner_name'] or item['owner_email']}" if escalation and (item["owner_name"] or item["owner_email"]) else "") if x]
        detail = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"- {item['name']}{detail}: {_when(item)} ({item['expires_on']})")
        link = (links or {}).get(item["id"]) if not escalation else None
        if link:
            lines.append(f"  I'm on it, pause reminders for 7 days: {link}")
        extra_html = " <span style='color:#6b7280'>(" + escape(", ".join(extras)) + ")</span>" if extras else ""
        if link:
            extra_html += f"<br><a href='{escape(link, quote=True)}' style='font-size:12px'>I'm on it &ndash; pause reminders for 7 days</a>"
        colour = "#b91c1c" if item["days_left"] < 0 else "#92400e"
        rows.append(
            "<tr>"
            + f"<td style='{cell}'>{escape(item['name'])}{extra_html}</td>"
            + f"<td style='{cell}'>{escape(item['category'])}</td>"
            + f"<td style='{cell};color:{colour}'>{escape(_when(item))}</td>"
            + f"<td style='{cell}'>{escape(item['expires_on'])}</td></tr>"
        )
    lines += ["", footer, f"-- {app_name}"]
    head = "padding:6px 10px"
    html = (
        "<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;color:#111827'>"
        f"<h2 style='margin:0 0 8px'>{escape(subject)}</h2>"
        "<table style='border-collapse:collapse;font-size:14px'><thead><tr>"
        f"<th align='left' style='{head}'>Item</th><th align='left' style='{head}'>Category</th>"
        f"<th align='left' style='{head}'>Status</th><th align='left' style='{head}'>Date</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        f"<p style='color:#6b7280;font-size:13px'>{footer}</p></div>"
    )
    return subject, "\n".join(lines), html


def run(
    conn: sqlite3.Connection,
    notifier: Notifier,
    today: date,
    *,
    default_recipient: str = "",
    app_name: str = "ThingsExpireTracker",
    dry_run: bool = False,
    escalate_to: str = "",
    webhook: Optional[Notifier] = None,
    currency: str = "",
    link_for: Optional[Callable[[dict[str, Any]], str]] = None,
) -> RunReport:
    """One pass. Safe to run as often as you like: a reminder that was delivered is never sent twice for the
    same expiry date, stage and recipient, and a failed one is retried on the next pass."""
    pending, orphans = due_reminders(conn, today, default_recipient, escalate_to=escalate_to, webhook=webhook is not None)
    report = RunReport(no_recipient=orphans)
    if dry_run:
        report.planned = pending
        return report

    groups: dict[tuple[str, bool], list[Pending]] = {}
    for p in pending:
        groups.setdefault((p.recipient.lower(), p.escalation), []).append(p)

    digest: dict[int, Pending] = {}
    for (_, escalation), group in groups.items():
        links = {p.item["id"]: link_for(p.item) for p in group} if (link_for and not escalation and group[0].recipient) else None
        subject, text, html = build_message(group, app_name, escalation=escalation, currency=currency, links=links)
        direct = bool(group[0].recipient)  # False: this group exists only for the webhook channel
        target = notifier if direct else webhook
        error: Optional[str] = None
        try:
            target.send(to=group[0].recipient or WEBHOOK_RECIPIENT, subject=subject, text=text, html=html)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - a failed delivery must never stop the other recipients
            error = str(exc) or exc.__class__.__name__
        for p in group:
            store.log_reminder(conn, p.item["id"], p.item["expires_on"], p.stage, p.log_recipient, error is None, error or "")
        if error is None:
            if direct:
                report.sent += 1
                report.items_notified += len(group)
                for p in group:
                    digest.setdefault(p.item["id"], p)
            else:
                report.webhook_sent += 1
                report.items_notified += len(group)
        else:
            report.failed += 1

    if webhook is not None and digest:
        subject, text, html = build_message(list(digest.values()), app_name, currency=currency)
        try:
            webhook.send(to=WEBHOOK_RECIPIENT, subject=subject, text=text, html=html)
            report.webhook_sent += 1
        except Exception:  # noqa: BLE001 - the channel post is a courtesy copy; email already went out
            report.webhook_failed += 1
    return report
