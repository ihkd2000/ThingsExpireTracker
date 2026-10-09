"""Files attached to items. The file is kept on disk and its details in SQLite."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from typing import Any, Optional

from . import store

MAX_BYTES = 10 * 1024 * 1024
KINDS = ("document", "invoice")
# extension -> (content type, signature check)
_TYPES = {
    ".pdf": ("application/pdf", lambda b: b.startswith(b"%PDF-")),
    ".png": ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ".txt": ("text/plain", lambda b: _is_text(b)),
    ".csv": ("text/csv", lambda b: _is_text(b)),
}


class AttachmentError(Exception):
    pass


def _is_text(data: bytes) -> bool:
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in data


def clean_filename(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name).strip(" .")
    return name[:120] or "file"


def detect_type(filename: str, data: bytes) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _TYPES:
        raise AttachmentError("Allowed file types: " + ", ".join(sorted(_TYPES)) + ".")
    content_type, looks_right = _TYPES[ext]
    if not data:
        raise AttachmentError("The file is empty.")
    if not looks_right(data):
        raise AttachmentError(f"The content does not look like a {ext} file.")
    return content_type


def files_dir(db_path: str, configured: str = "") -> str:
    return configured or (os.path.abspath(db_path) + ".files")


def save(conn: sqlite3.Connection, directory: str, item_id: int, filename: str, data: bytes, *,
         kind: str = "document", actor: str = "system", max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    if kind not in KINDS:
        raise AttachmentError("Kind must be document or invoice.")
    if len(data) > max_bytes:
        raise AttachmentError(f"Files can be at most {max_bytes // (1024 * 1024)} MB.")
    store.get_item(conn, item_id)
    filename = clean_filename(filename)
    content_type = detect_type(filename, data)
    os.makedirs(directory, exist_ok=True)
    stored_name = secrets.token_hex(16)  # never derived from user input, so no path tricks are possible
    with open(os.path.join(directory, stored_name), "wb") as handle:
        handle.write(data)
    cursor = conn.execute(
        "INSERT INTO attachments (item_id, kind, filename, stored_name, content_type, size, sha256, uploaded_by, uploaded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, kind, filename, stored_name, content_type, len(data), hashlib.sha256(data).hexdigest(), actor[:40], store.now_text()),
    )
    store.record(conn, item_id, "attached", f"{kind}: {filename}", actor)
    conn.commit()
    return get(conn, cursor.lastrowid)


def get(conn: sqlite3.Connection, attachment_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise store.NotFound(f"Attachment {attachment_id} was not found.")
    return dict(row)


def list_for(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM attachments WHERE item_id = ? ORDER BY id DESC", (item_id,)).fetchall()
    return [dict(r) for r in rows]


def read(conn: sqlite3.Connection, directory: str, attachment_id: int) -> tuple[dict[str, Any], bytes]:
    meta = get(conn, attachment_id)
    path = os.path.join(directory, meta["stored_name"])
    try:
        with open(path, "rb") as handle:
            return meta, handle.read()
    except OSError:
        raise store.NotFound("The stored file is missing from disk.") from None


def delete(conn: sqlite3.Connection, directory: str, attachment_id: int, actor: str = "system") -> None:
    meta = get(conn, attachment_id)
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    store.record(conn, meta["item_id"], "detached", meta["filename"], actor)
    conn.commit()
    try:
        os.remove(os.path.join(directory, meta["stored_name"]))
    except OSError:
        pass
