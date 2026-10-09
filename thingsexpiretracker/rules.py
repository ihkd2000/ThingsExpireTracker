"""Expiry and reminder-stage calculations."""
from __future__ import annotations

import calendar
from datetime import date
from typing import Iterable, Optional

DEFAULT_LEADS = (30, 14, 7, 1)
MAX_LEADS = 10
MAX_LEAD_DAYS = 365

STATUS_OK = "ok"
STATUS_DUE_SOON = "due_soon"
STATUS_DUE_TODAY = "due_today"
STATUS_EXPIRED = "expired"

STATUS_LABELS = {
    STATUS_OK: "OK",
    STATUS_DUE_SOON: "Due soon",
    STATUS_DUE_TODAY: "Due today",
    STATUS_EXPIRED: "Expired",
}

# Used to put overdue items ahead of upcoming renewals.
STATUS_ORDER = {STATUS_EXPIRED: 0, STATUS_DUE_TODAY: 1, STATUS_DUE_SOON: 2, STATUS_OK: 3}


def parse_leads(text: Optional[str]) -> tuple[int, ...]:
    """'30, 14,7' -> (30, 14, 7). Blank gives the defaults. Raises ValueError on bad input."""
    if text is None or not str(text).strip():
        return DEFAULT_LEADS
    values: list[int] = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"'{part}' is not a whole number of days.")
        number = int(part)
        if not 1 <= number <= MAX_LEAD_DAYS:
            raise ValueError(f"Reminder days must be between 1 and {MAX_LEAD_DAYS}.")
        values.append(number)
    if not values:
        return DEFAULT_LEADS
    unique = sorted(set(values), reverse=True)
    if len(unique) > MAX_LEADS:
        raise ValueError(f"Use at most {MAX_LEADS} reminder stages.")
    return tuple(unique)


def format_leads(leads: Iterable[int]) -> str:
    return ",".join(str(n) for n in leads)


def days_left(expires_on: date, today: date) -> int:
    return (expires_on - today).days


def status(expires_on: date, today: date, leads: Iterable[int] = DEFAULT_LEADS) -> str:
    remaining = days_left(expires_on, today)
    if remaining < 0:
        return STATUS_EXPIRED
    if remaining == 0:
        return STATUS_DUE_TODAY
    configured_leads = tuple(leads)
    if remaining <= max(configured_leads or DEFAULT_LEADS):
        return STATUS_DUE_SOON
    return STATUS_OK


def reminder_stage(remaining: int, leads: Iterable[int] = DEFAULT_LEADS) -> Optional[int]:
    """Return the current reminder stage, or None if no stage has been reached.

    A missed run does not queue every skipped stage. Overdue stages use
    negative week numbers (-1 for days 1-7, -2 for days 8-14, etc.).
    """
    if remaining < 0:
        overdue = -remaining
        return -(1 + (overdue - 1) // 7)
    reached = [lead for lead in (*tuple(leads), 0) if remaining <= lead]
    return min(reached) if reached else None


def stage_label(stage: int) -> str:
    if stage < 0:
        return "overdue"
    if stage == 0:
        return "expires today"
    return f"{stage} day{'s' if stage != 1 else ''} or less to go"


def add_months(start: date, months: int) -> date:
    """Add calendar months, clamping dates that the target month lacks."""
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
