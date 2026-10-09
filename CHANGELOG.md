# Changelog

## 1.2.0
- **Users and roles** (viewer, editor, admin): sign-in on the dashboard, scrypt-hashed passwords, 12-hour sessions, login
  throttling, last-admin protection. The audit trail now names the person. `thingsexpiretracker user add/list/passwd/role/disable`.
  The shared API token keeps working (as an admin) for scripts.
- **File attachments** on items (PDF, PNG, JPG, TXT, CSV; 10 MB; content checked, stored under random names).
- **Invoice upload and reading.** The file is read into a draft (vendor, number, dates, amount, currency), matched to an
  item by reference/vendor/name, and recorded only after a person confirms it. Reading works for text PDFs and text files out
  of the box, for scans and photos with `tesseract` or `pdftotext` installed, or with Claude when `THINGSEXPIRE_ANTHROPIC_KEY`
  is set (opt-in; the file is sent to the Anthropic API). Confirming can also update the item's cost and vendor.
- **Snooze and "I'm on it" links** in reminder emails (signed, expiring, and invalidated by renewal); a confirmation
  page protects against mail scanners. Escalation is never snoozed.
- **Renewal progress** (quote requested, awaiting approval, approved, ordered): reminders pause until the expiry date.
  Renewing clears both pauses.
- Fix: a database that fails to open (for example one created by a newer version) no longer leaks its connection.
- Schema v3; databases from 1.0 and 1.1 upgrade in place.

## 1.1.0
- Cost and vendor on every item, with a spend forecast (overdue, next 30/90/365 days). Money is stored as whole cents.
- Audit trail: who changed what and when (`thingsexpiretracker history`, shown in the dashboard).
- Calendar feed (`thingsexpiretracker ics`, `/api/calendar.ics`) with an alarm for each reminder day.
- Slack/Teams-style webhook channel; items with no owner email are delivered there instead of being skipped.
- Escalation: a second contact is told when an item has been overdue for more than a week.
- Reminders are now de-duplicated per recipient, so a new owner is told about an item the previous owner already heard about.
- Redesigned dashboard: detail panel with editing, renewal and history, sortable columns, category and status filters,
  CSV import/export, reminder preview, dark mode, mobile layout.
- CLI: `summary`, `history`, `ics`, `backup`, `--json` on list/show/summary, `--vendor`, `--cost`.
- Database upgrades itself from 1.0 (schema v2). Dockerfile added.

## 1.0.0
- First release: items, renewals, reminders by email, CSV import/export, CLI, dashboard and JSON API.
