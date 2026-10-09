"""Calendar export: one all-day event per item, with an alarm for each reminder day."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional


def _escape(text: str) -> str:
    bs = chr(92)
    text = text.replace(bs, bs + bs).replace(";", bs + ";").replace(",", bs + ",")
    return text.replace("\r\n", bs + "n").replace("\n", bs + "n").replace("\r", bs + "n")


def _fold(line: str) -> list[str]:
    """Lines are limited to 75 octets; continuation lines start with one space. Never split a UTF-8 character."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return [line]
    parts: list[str] = []
    limit = 75
    while data:
        cut = min(limit, len(data))
        while cut < len(data) and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(data[:cut].decode("utf-8"))
        data = data[cut:]
        limit = 74
    return [parts[0]] + [" " + p for p in parts[1:]]


def build_calendar(items: Iterable[dict[str, Any]], app_name: str = "ThingsExpireTracker", now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{app_name}//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_escape(app_name)}"]
    for item in items:
        day = date.fromisoformat(item["expires_on"])
        description = [f"Category: {item['category']}"]
        for label, key in (("Reference", "reference"), ("Vendor", "vendor"), ("Owner", "owner_name"), ("Notes", "notes")):
            if item.get(key):
                description.append(f"{label}: {item[key]}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:item-{item['id']}-{day.strftime('%Y%m%d')}@thingsexpiretracker",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{date.fromordinal(day.toordinal() + 1).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape('Expires: ' + item['name'])}",
            f"DESCRIPTION:{_escape(chr(10).join(description))}",
            "TRANSPARENCY:TRANSPARENT",
        ]
        for lead in sorted({n for n in item["lead_days"] if n > 0}, reverse=True):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(item['name'] + ' expires in ' + str(lead) + ' days')}",
                      f"TRIGGER:-P{lead}D", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return "\r\n".join(folded) + "\r\n"
