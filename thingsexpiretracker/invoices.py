"""Invoices are uploaded, read into a draft, checked by a person and then recorded."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Optional

from . import attachments, extract, store
from .config import Settings

STATUSES = ("draft", "confirmed")
WORKFLOWS = ("", "quote_requested", "awaiting_approval", "approved", "ordered")


def _present(row: sqlite3.Row) -> dict[str, Any]:
    inv = dict(row)
    inv["amount"] = store.format_cost(inv.pop("amount_cents"))
    inv["confidence"] = json.loads(inv["confidence"] or "{}")
    return inv


def get(conn: sqlite3.Connection, invoice_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise store.NotFound(f"Invoice {invoice_id} was not found.")
    return _present(row)


def list_invoices(conn: sqlite3.Connection, *, item_id: Optional[int] = None, status: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM invoices WHERE 1=1", []
    if item_id is not None:
        sql += " AND item_id = ?"
        args.append(item_id)
    if status:
        sql += " AND status = ?"
        args.append(status)
    rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    return [_present(r) for r in rows]


def suggest_item(conn: sqlite3.Connection, text: str, fields: dict[str, Any]) -> Optional[int]:
    """Best guess at which item an invoice belongs to: a reference number or vendor/name found in the document."""
    haystack = (text + " " + " ".join(str(v) for v in fields.values())).lower()
    best: Optional[tuple[int, int]] = None
    for item in store.list_items(conn):
        score = 0
        if item["reference"] and item["reference"].lower() in haystack:
            score += 5
        if item["vendor"] and item["vendor"].lower() in haystack:
            score += 3
        if len(item["name"]) >= 4 and item["name"].lower() in haystack:
            score += 2
        if score and (best is None or score > best[0]):
            best = (score, item["id"])
    return best[1] if best else None


def ingest(conn: sqlite3.Connection, directory: str, settings: Settings, filename: str, data: bytes, *,
           item_id: Optional[int] = None, actor: str = "system") -> dict[str, Any]:
    """Stores the file, reads it and saves a draft. If no item is given, the best match (if any) is suggested."""
    content_type = attachments.detect_type(attachments.clean_filename(filename), data)  # validate before touching the disk
    result = extract.read_invoice(data, filename, content_type, settings, [i["vendor"] for i in store.list_items(conn) if i["vendor"]])
    fields = result["fields"]
    target = item_id if item_id is not None else suggest_item(conn, result["text"], fields)
    if target is None:
        raise InvoiceNeedsItem(result)
    attachment = attachments.save(conn, directory, target, filename, data, kind="invoice", actor=actor)
    cents = None
    try:
        cents = store.parse_cost(fields.get("amount"))
    except ValueError:
        pass
    cursor = conn.execute(
        "INSERT INTO invoices (item_id, attachment_id, status, vendor, invoice_number, invoice_date, due_date, amount_cents,"
        " currency, source, confidence, suggested_item, created_by, created_at) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (target, attachment["id"], fields.get("vendor", "")[:120], fields.get("invoice_number", "")[:60],
         fields.get("invoice_date", ""), fields.get("due_date", ""), cents, fields.get("currency", "")[:8],
         result["source"], json.dumps(result["confidence"]), None if item_id is not None else target, actor[:40], store.now_text()),
    )
    conn.commit()
    invoice = get(conn, cursor.lastrowid)
    invoice["notes"] = result["notes"]
    return invoice


class InvoiceNeedsItem(Exception):
    """The file was read but no item matches; the caller must say which item it belongs to."""

    def __init__(self, result: dict[str, Any]):
        super().__init__("Choose which item this invoice belongs to.")
        self.result = result


def _clean(data: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    merged = {k: data.get(k, current.get(k, "")) for k in ("vendor", "invoice_number", "invoice_date", "due_date", "amount", "currency")}
    out: dict[str, Any] = {
        "vendor": str(merged["vendor"] or "").strip()[:120],
        "invoice_number": str(merged["invoice_number"] or "").strip()[:60],
        "currency": str(merged["currency"] or "").strip().upper()[:8],
    }
    for key in ("invoice_date", "due_date"):
        value = str(merged[key] or "").strip()
        if value:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                errors[key] = "Use the format YYYY-MM-DD."
        out[key] = value
    try:
        out["amount_cents"] = store.parse_cost(merged["amount"])
    except ValueError as exc:
        errors["amount"] = str(exc)
    if errors:
        raise store.ValidationError(errors)
    return out


def update_draft(conn: sqlite3.Connection, invoice_id: int, data: dict[str, Any]) -> dict[str, Any]:
    invoice = get(conn, invoice_id)
    if invoice["status"] != "draft":
        raise store.ValidationError({"status": "A confirmed invoice can no longer be edited."})
    clean = _clean(data, invoice)
    target = invoice["item_id"]
    if data.get("item_id") not in (None, ""):
        target = int(data["item_id"])
        store.get_item(conn, target)
    conn.execute(
        "UPDATE invoices SET vendor=:vendor, invoice_number=:invoice_number, invoice_date=:invoice_date, due_date=:due_date,"
        " amount_cents=:amount_cents, currency=:currency, item_id=:item_id WHERE id=:id",
        {**clean, "item_id": target, "id": invoice_id},
    )
    if target != invoice["item_id"] and invoice["attachment_id"]:
        conn.execute("UPDATE attachments SET item_id = ? WHERE id = ?", (target, invoice["attachment_id"]))
    conn.commit()
    return get(conn, invoice_id)


def confirm(conn: sqlite3.Connection, invoice_id: int, *, update_cost: bool = False, vendor_to_item: bool = False,
            actor: str = "system") -> dict[str, Any]:
    """Records the invoice. Optionally copies its amount to the item's renewal cost and its vendor to the item."""
    invoice = get(conn, invoice_id)
    if invoice["status"] == "confirmed":
        return invoice
    if invoice["item_id"] is None:
        raise store.ValidationError({"item_id": "Choose an item first."})
    if not invoice["amount"]:
        raise store.ValidationError({"amount": "Enter the amount before confirming."})
    conn.execute("UPDATE invoices SET status = 'confirmed', confirmed_at = ? WHERE id = ?", (store.now_text(), invoice_id))
    detail = f"invoice {invoice['invoice_number'] or '#' + str(invoice_id)}: {invoice['amount']} {invoice['currency']}".strip()
    store.record(conn, invoice["item_id"], "invoice", detail, actor)
    conn.commit()
    changes: dict[str, Any] = {}
    if update_cost:
        changes["cost"] = invoice["amount"]
    if vendor_to_item and invoice["vendor"]:
        changes["vendor"] = invoice["vendor"]
    if changes:
        store.update_item(conn, invoice["item_id"], changes, actor=actor)
    return get(conn, invoice_id)


def discard(conn: sqlite3.Connection, directory: str, invoice_id: int, actor: str = "system") -> None:
    invoice = get(conn, invoice_id)
    if invoice["status"] != "draft":
        raise store.ValidationError({"status": "Only drafts can be discarded."})
    conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    conn.commit()
    if invoice["attachment_id"]:
        try:
            attachments.delete(conn, directory, invoice["attachment_id"], actor)
        except store.NotFound:
            pass


def total_for(conn: sqlite3.Connection, item_id: int) -> int:
    row = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM invoices WHERE item_id = ? AND status = 'confirmed'", (item_id,)).fetchone()
    return int(row[0])
