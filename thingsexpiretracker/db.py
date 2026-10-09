"""SQLite connection and schema. The schema version is kept in PRAGMA user_version."""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

_SCHEMA_V1 = """
CREATE TABLE items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'General',
    owner_name  TEXT NOT NULL DEFAULT '',
    owner_email TEXT NOT NULL DEFAULT '',
    reference   TEXT NOT NULL DEFAULT '',
    expires_on  TEXT NOT NULL,
    lead_days   TEXT NOT NULL DEFAULT '30,14,7,1',
    notes       TEXT NOT NULL DEFAULT '',
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX ix_items_expires_on ON items (expires_on);
CREATE INDEX ix_items_category ON items (category);

CREATE TABLE renewals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    old_expires_on  TEXT NOT NULL,
    new_expires_on  TEXT NOT NULL,
    renewed_at      TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE reminder_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    expires_on  TEXT NOT NULL,
    stage       INTEGER NOT NULL,
    recipient   TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_reminder_log_item ON reminder_log (item_id, expires_on, stage);
"""

_SCHEMA_V2 = """
ALTER TABLE items ADD COLUMN vendor TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN cost_cents INTEGER;

CREATE TABLE audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    action   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT '',
    actor    TEXT NOT NULL DEFAULT 'system',
    at       TEXT NOT NULL
);
CREATE INDEX ix_audit_item ON audit_log (item_id, id);
"""

_SCHEMA_V3 = """
ALTER TABLE items ADD COLUMN snoozed_until TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN workflow TEXT NOT NULL DEFAULT '';

CREATE TABLE attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT 'document',
    filename     TEXT NOT NULL,
    stored_name  TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    uploaded_by  TEXT NOT NULL DEFAULT 'system',
    uploaded_at  TEXT NOT NULL
);
CREATE INDEX ix_attachments_item ON attachments (item_id);

CREATE TABLE invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        INTEGER REFERENCES items(id) ON DELETE SET NULL,
    attachment_id  INTEGER REFERENCES attachments(id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'draft',
    vendor         TEXT NOT NULL DEFAULT '',
    invoice_number TEXT NOT NULL DEFAULT '',
    invoice_date   TEXT NOT NULL DEFAULT '',
    due_date       TEXT NOT NULL DEFAULT '',
    amount_cents   INTEGER,
    currency       TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    confidence     TEXT NOT NULL DEFAULT '{}',
    suggested_item INTEGER,
    created_by     TEXT NOT NULL DEFAULT 'system',
    created_at     TEXT NOT NULL,
    confirmed_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_invoices_item ON invoices (item_id);

CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
"""

_MIGRATIONS = ((1, _SCHEMA_V1), (2, _SCHEMA_V2), (3, _SCHEMA_V3))


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"This database was created by a newer version of ThingsExpireTracker (schema {version}, this build knows {SCHEMA_VERSION})."
        )
    for target, script in _MIGRATIONS:
        if version < target:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
