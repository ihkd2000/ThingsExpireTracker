"""Reads and writes items, renewals and the reminder log. Each function takes an open connection."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from . import rules

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIELDS = ("name", "category", "vendor", "owner_name", "owner_email", "reference", "expires_on", "lead_days", "cost", "notes")


class ValidationError(Exception):
    """Carries a dict of field -> message."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class NotFound(Exception):
    pass


def now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("Use the format YYYY-MM-DD.") from None


def clean_item(data: dict[str, Any], *, partial_from: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Validates and normalises item input. With ``partial_from`` missing fields keep their current value."""
    merged = dict(partial_from or {})
    merged.update({k: data[k] for k in FIELDS if k in data})
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}

    def text(field: str, limit: int, *, required: bool = False, default: str = "") -> str:
        value = str(merged.get(field) if merged.get(field) is not None else default).strip()
        if required and not value:
            errors[field] = "This is required."
        elif len(value) > limit:
            errors[field] = f"Keep this under {limit} characters."
        return value

    out["name"] = text("name", 120, required=True)
    out["category"] = text("category", 60, default="General") or "General"
    out["vendor"] = text("vendor", 120)
    out["owner_name"] = text("owner_name", 120)
    out["owner_email"] = text("owner_email", 200)
    out["reference"] = text("reference", 100)
    out["notes"] = text("notes", 1000)

    if out["owner_email"] and not _EMAIL.match(out["owner_email"]):
        errors["owner_email"] = "Enter a valid email address."

    try:
        out["expires_on"] = parse_date(merged.get("expires_on")).isoformat()
    except ValueError as exc:
        errors["expires_on"] = str(exc) if merged.get("expires_on") else "This is required."

    try:
        raw = merged.get("lead_days")
        leads = rules.parse_leads(",".join(map(str, raw)) if isinstance(raw, (list, tuple)) else raw)
        out["lead_days"] = rules.format_leads(leads)
    except ValueError as exc:
        errors["lead_days"] = str(exc)

    try:
        out["cost_cents"] = parse_cost(merged.get("cost"))
    except ValueError as exc:
        errors["cost"] = str(exc)

    if errors:
        raise ValidationError(errors)
    return out


def parse_cost(value: Any) -> Optional[int]:
    """'1200.50' -> 120050 (cents). Empty means no cost recorded. Money is kept as whole cents, never floats."""
    if value is None or isinstance(value, bool):
        if value is None:
            return None
        raise ValueError("Enter an amount such as 1200 or 1200.50.")
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError("Enter an amount such as 1200 or 1200.50.") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000"):
        raise ValueError("Enter an amount between 0 and 1,000,000,000.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_cost(cents: Optional[int]) -> str:
    return "" if cents is None else f"{cents // 100}.{cents % 100:02d}"


def _present(row: sqlite3.Row, today: date) -> dict[str, Any]:
    item = dict(row)
    item["archived"] = bool(item["archived"])
    item["cost"] = format_cost(item.pop("cost_cents", None))
    leads = rules.parse_leads(item["lead_days"])
    expires = date.fromisoformat(item["expires_on"])
    item["lead_days"] = list(leads)
    item["days_left"] = rules.days_left(expires, today)
    item["status"] = rules.status(expires, today, leads)
    item["status_label"] = rules.STATUS_LABELS[item["status"]]
    return item


def record(conn: sqlite3.Connection, item_id: int, action: str, detail: str = "", actor: str = "system") -> None:
    """Appends to the audit trail. Callers commit, so the entry lands in the same transaction as the change."""
    conn.execute(
        "INSERT INTO audit_log (item_id, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
        (item_id, action, detail[:500], actor[:40], now_text()),
    )


def history_for(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM audit_log WHERE item_id = ? ORDER BY id DESC", (item_id,)).fetchall()
    return [dict(r) for r in rows]


def recent_activity(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.*, i.name AS item_name FROM audit_log a JOIN items i ON i.id = a.item_id ORDER BY a.id DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return [dict(r) for r in rows]


def create_item(conn: sqlite3.Connection, data: dict[str, Any], today: Optional[date] = None, actor: str = "system") -> dict[str, Any]:
    clean = clean_item(data)
    cursor = conn.execute(
        "INSERT INTO items (name, category, vendor, owner_name, owner_email, reference, expires_on, lead_days, cost_cents, notes, created_at)"
        " VALUES (:name, :category, :vendor, :owner_name, :owner_email, :reference, :expires_on, :lead_days, :cost_cents, :notes, :created_at)",
        {**clean, "created_at": now_text()},
    )
    record(conn, cursor.lastrowid, "created", f"expires {clean['expires_on']}", actor)
    conn.commit()
    return get_item(conn, cursor.lastrowid, today)


def get_item(conn: sqlite3.Connection, item_id: int, today: Optional[date] = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFound(f"Item {item_id} was not found.")
    return _present(row, today or date.today())


def list_items(
    conn: sqlite3.Connection,
    *,
    today: Optional[date] = None,
    query: str = "",
    category: str = "",
    status: str = "",
    include_archived: bool = False,
    within_days: Optional[int] = None,
) -> list[dict[str, Any]]:
    today = today or date.today()
    rows = conn.execute("SELECT * FROM items" + ("" if include_archived else " WHERE archived = 0")).fetchall()
    items = [_present(r, today) for r in rows]

    term = query.strip().lower()
    if term:
        items = [
            i for i in items
            if term in " ".join(str(i[f]) for f in ("name", "category", "owner_name", "owner_email", "reference", "notes")).lower()
        ]
    if category:
        items = [i for i in items if i["category"].lower() == category.strip().lower()]
    if status:
        items = [i for i in items if i["status"] == status]
    if within_days is not None:
        items = [i for i in items if i["days_left"] <= within_days]
    items.sort(key=lambda i: (i["days_left"], i["name"].lower()))
    return items


def update_item(conn: sqlite3.Connection, item_id: int, data: dict[str, Any], today: Optional[date] = None, actor: str = "system") -> dict[str, Any]:
    current = get_item(conn, item_id, today)
    base = {f: current[f] for f in FIELDS}
    base["lead_days"] = rules.format_leads(current["lead_days"])
    clean = clean_item(data, partial_from=base)
    conn.execute(
        "UPDATE items SET name=:name, category=:category, vendor=:vendor, owner_name=:owner_name,"
        " owner_email=:owner_email, reference=:reference, expires_on=:expires_on, lead_days=:lead_days,"
        " cost_cents=:cost_cents, notes=:notes WHERE id=:id",
        {**clean, "id": item_id},
    )
    changes = []
    for field in FIELDS:
        if field == "notes":
            if clean["notes"] != current["notes"]:
                changes.append("notes")
            continue
        old = base[field]
        new = clean[field] if field != "cost" else format_cost(clean["cost_cents"])
        if str(old) != str(new):
            changes.append(f"{field}: {old or '-'} -> {new or '-'}")
    if changes:
        record(conn, item_id, "updated", "; ".join(changes), actor)
    conn.commit()
    return get_item(conn, item_id, today)


def set_archived(conn: sqlite3.Connection, item_id: int, archived: bool = True, actor: str = "system") -> None:
    get_item(conn, item_id)
    conn.execute("UPDATE items SET archived = ? WHERE id = ?", (1 if archived else 0, item_id))
    record(conn, item_id, "archived" if archived else "restored", "", actor)
    conn.commit()


def renew_item(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    new_expires_on: Any = None,
    months: Optional[int] = None,
    note: str = "",
    today: Optional[date] = None,
    actor: str = "system",
) -> dict[str, Any]:
    """Moves the expiry date forward and records the renewal. Reminder history is per expiry date, so the
    next cycle starts fresh without any clean-up."""
    item = get_item(conn, item_id, today)
    old = date.fromisoformat(item["expires_on"])
    if new_expires_on:
        try:
            new = parse_date(new_expires_on)
        except ValueError as exc:
            raise ValidationError({"new_expires_on": str(exc)}) from None
    elif months:
        if months < 1 or months > 240:
            raise ValidationError({"months": "Choose between 1 and 240 months."})
        # Renewing early extends from the old expiry (no paid time is lost); a lapsed item restarts from today.
        base = old if old >= (today or date.today()) else (today or date.today())
        new = rules.add_months(base, months)
    else:
        raise ValidationError({"new_expires_on": "Give a new date or a number of months."})
    if new <= old:
        raise ValidationError({"new_expires_on": "The new date must be later than the current one."})

    conn.execute("UPDATE items SET expires_on = ?, snoozed_until = '', workflow = '' WHERE id = ?", (new.isoformat(), item_id))
    conn.execute(
        "INSERT INTO renewals (item_id, old_expires_on, new_expires_on, renewed_at, note) VALUES (?, ?, ?, ?, ?)",
        (item_id, old.isoformat(), new.isoformat(), now_text(), note.strip()[:300]),
    )
    record(conn, item_id, "renewed", f"{old.isoformat()} -> {new.isoformat()}", actor)
    conn.commit()
    return get_item(conn, item_id, today)


def renewals_for(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM renewals WHERE item_id = ? ORDER BY id DESC", (item_id,)).fetchall()
    return [dict(r) for r in rows]


def summary(conn: sqlite3.Connection, today: Optional[date] = None) -> dict[str, Any]:
    today = today or date.today()
    items = list_items(conn, today=today)
    counts = {key: 0 for key in rules.STATUS_LABELS}
    categories: dict[str, int] = {}
    spend = {"overdue": 0, "next_30": 0, "next_90": 0, "next_365": 0}  # in cents; only items with a cost
    for item in items:
        counts[item["status"]] += 1
        categories[item["category"]] = categories.get(item["category"], 0) + 1
        if item["cost"]:
            cents = parse_cost(item["cost"]) or 0
            left = item["days_left"]
            if left < 0:
                spend["overdue"] += cents
            for horizon in (30, 90, 365):
                if 0 <= left <= horizon:
                    spend[f"next_{horizon}"] += cents
    return {
        "total": len(items),
        "counts": counts,
        "categories": dict(sorted(categories.items())),
        "spend_cents": spend,
        "next": [i for i in items if i["status"] != rules.STATUS_OK][:10],
    }


# ---- reminder log ----------------------------------------------------------------------------------------------------

def reminder_already_sent(conn: sqlite3.Connection, item_id: int, expires_on: str, stage: int, recipient: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reminder_log WHERE item_id = ? AND expires_on = ? AND stage = ? AND lower(recipient) = lower(?) AND ok = 1 LIMIT 1",
        (item_id, expires_on, stage, recipient),
    ).fetchone()
    return row is not None


def log_reminder(conn: sqlite3.Connection, item_id: int, expires_on: str, stage: int, recipient: str, ok: bool, error: str = "") -> None:
    conn.execute(
        "INSERT INTO reminder_log (item_id, expires_on, stage, recipient, sent_at, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, expires_on, stage, recipient, now_text(), 1 if ok else 0, error[:500]),
    )
    conn.commit()


def recent_reminders(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.*, i.name AS item_name FROM reminder_log r JOIN items i ON i.id = r.item_id ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def reminders_for(conn: sqlite3.Connection, item_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM reminder_log WHERE item_id = ? ORDER BY id DESC LIMIT ?", (item_id, limit)).fetchall()
    return [dict(r) for r in rows]


MAX_SNOOZE_DAYS = 30
WORKFLOWS = ("", "quote_requested", "awaiting_approval", "approved", "ordered")


def snooze_item(conn: sqlite3.Connection, item_id: int, days: int, today: Optional[date] = None, actor: str = "system") -> dict[str, Any]:
    """Pauses owner reminders for ``days`` days (0 clears it). Escalation to the second contact is not paused."""
    today = today or date.today()
    get_item(conn, item_id, today)
    if isinstance(days, bool) or not isinstance(days, int) or days < 0 or days > MAX_SNOOZE_DAYS:
        raise ValidationError({"days": f"Choose between 0 and {MAX_SNOOZE_DAYS} days."})
    until = (today + timedelta(days=days)).isoformat() if days else ""
    conn.execute("UPDATE items SET snoozed_until = ? WHERE id = ?", (until, item_id))
    record(conn, item_id, "snoozed" if days else "unsnoozed", f"until {until}" if days else "", actor)
    conn.commit()
    return get_item(conn, item_id, today)


def set_workflow(conn: sqlite3.Connection, item_id: int, state: str, today: Optional[date] = None, actor: str = "system") -> dict[str, Any]:
    """Marks a renewal as in progress. Until the item actually expires, in-progress items are not nagged about."""
    get_item(conn, item_id, today)
    if state not in WORKFLOWS:
        raise ValidationError({"workflow": "Choose one of: " + ", ".join(w or "none" for w in WORKFLOWS) + "."})
    conn.execute("UPDATE items SET workflow = ? WHERE id = ?", (state, item_id))
    record(conn, item_id, "workflow", state or "cleared", actor)
    conn.commit()
    return get_item(conn, item_id, today)
