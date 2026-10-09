"""CSV import and export, for people who already track things in a spreadsheet."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, TextIO

from . import rules, store

COLUMNS = ("name", "category", "vendor", "owner_name", "owner_email", "reference", "expires_on", "lead_days", "cost", "notes")
REQUIRED = ("name", "expires_on")


@dataclass
class ImportReport:
    imported: int = 0
    skipped_duplicates: int = 0
    errors: list[str] = field(default_factory=list)  # "line 4: expires_on: Use the format YYYY-MM-DD."


def _normalise_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_").lstrip("﻿")


def import_csv(conn: sqlite3.Connection, stream: TextIO, *, strict: bool = False, actor: str = "import") -> ImportReport:
    """Imports rows. Rows with problems are reported with their line number and skipped.
    With ``strict`` nothing is saved if any row has a problem."""
    report = ImportReport()
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        report.errors.append("The file is empty.")
        return report
    reader.fieldnames = [_normalise_header(n) for n in reader.fieldnames]
    missing = [c for c in REQUIRED if c not in reader.fieldnames]
    if missing:
        report.errors.append("Missing column(s): " + ", ".join(missing) + ". Expected: " + ", ".join(COLUMNS) + ".")
        return report

    existing = {
        (i["name"].lower(), i["reference"].lower(), i["expires_on"])
        for i in store.list_items(conn, include_archived=True)
    }
    pending: list[dict] = []
    for row in reader:
        line = reader.line_num
        data = {k: (v or "").strip() for k, v in row.items() if k in COLUMNS}
        if not any(data.values()):
            continue
        try:
            clean = store.clean_item(data)
        except store.ValidationError as exc:
            for fld, message in exc.errors.items():
                report.errors.append(f"line {line}: {fld}: {message}")
            continue
        key = (clean["name"].lower(), clean["reference"].lower(), clean["expires_on"])
        if key in existing:
            report.skipped_duplicates += 1
            continue
        existing.add(key)
        pending.append(data)

    if strict and report.errors:
        return report
    for clean in pending:
        store.create_item(conn, clean, actor=actor)
        report.imported += 1
    return report


def export_csv(items: Iterable[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for item in items:
        row = []
        for column in COLUMNS:
            value = item[column]
            if column == "lead_days":
                value = rules.format_leads(value)
            row.append(_safe_cell(str(value)))
        writer.writerow(row)
    return buffer.getvalue()


def _safe_cell(value: str) -> str:
    """Stops spreadsheet programs from running a cell that starts with = + - @ as a formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
