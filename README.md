# ThingsExpireTracker

Small teams often keep licences, certificates, domains and insurance renewals in a spreadsheet and only notice a lapse after it has caused a problem. This tool keeps one list, reminds the person who owns each renewal, escalates when nobody acts, and records what each renewal cost.

ThingsExpireTracker tracks expiry dates for licences, warranties, certificates, contracts and similar records. It stores records in SQLite and provides a command-line interface and a local web dashboard. Reminders can be delivered by email or webhook.

The application uses Python 3.10+ and the standard library. OCR tools and the optional Anthropic integration are only needed for some invoice formats. The package version is 1.2.0.

**Before using it for important renewals:** Set up a daily reminder job, configure a delivery channel, and confirm that a test reminder reaches the intended recipient. Adding a record alone does not schedule background processing.

## Quick start

```bash
python -m thingsexpiretracker add "Business insurance" 2026-12-01 --category Insurance --owner Sam --email sam@example.com --vendor Acme --cost 4200
python -m thingsexpiretracker list --within 60          # add --json for scripts
python -m thingsexpiretracker summary                   # counts and the money coming due
python -m thingsexpiretracker renew 1 --months 12 --note "Invoice 118"
python -m thingsexpiretracker history 1                 # audit trail (no id = recent changes everywhere)
python -m thingsexpiretracker remind --dry-run          # show what would be sent
python -m thingsexpiretracker remind                    # send; schedule this daily (cron / Task Scheduler)
python -m thingsexpiretracker import items.csv          # name,category,vendor,owner_name,owner_email,reference,expires_on,lead_days,cost,notes
python -m thingsexpiretracker export > backup.csv
python -m thingsexpiretracker ics thingsexpiretracker.ics       # calendar file with an alarm for each reminder day
python -m thingsexpiretracker backup copy.db            # consistent copy of the database
python -m thingsexpiretracker snooze 1 7                # pause owner reminders for a week
python -m thingsexpiretracker workflow 1 approved       # renewal in progress (reminders pause until expiry)
python -m thingsexpiretracker attach 1 policy.pdf       # keep the document with the item
python -m thingsexpiretracker invoice add invoice.pdf   # read it into a draft; check, then:
python -m thingsexpiretracker invoice confirm 1 --update-cost
python -m thingsexpiretracker user add sam --role editor   # prompts for a password; roles: viewer, editor, admin
python -m thingsexpiretracker serve                     # dashboard at http://127.0.0.1:8080
```

## What it does

| Area | |
|---|---|
| Reminders | Stages at 30, 14, 7, 1 days and on the day (override per item with `--lead-days 60,30,7`), then weekly while overdue. |
| De-duplication | Each (item, expiry date, stage, recipient) is delivered once. Failed deliveries retry on the next run. A missed job sends only the current stage, not a backlog. |
| Grouping | One email per recipient listing all of their items, most urgent first. |
| Escalation | Set `THINGSEXPIRE_ESCALATE_TO` and that person also hears about anything overdue for more than a week. |
| Team channel | Set `THINGSEXPIRE_WEBHOOK_URL` (Slack, Mattermost, Teams workflow) to post a digest. Items with no owner email go there instead of being skipped. |
| Cost | Optional cost and vendor per item; the dashboard and `summary` show overdue and upcoming spend. Stored as whole cents. |
| Audit trail | Every create, edit, renewal and archive is recorded with the actor (`cli`, `api`, `import`). |
| Calendar | `thingsexpiretracker ics` or a subscribable feed at `/api/calendar.ics`. |
| Users and roles | Viewers read, editors change items/files/invoices and send reminders, admins also manage users. Until the first user exists the dashboard is open on localhost (or protected by `THINGSEXPIRE_TOKEN`). |
| Files | Attach certificates, policies and contracts to an item (PDF, PNG, JPG, TXT, CSV up to 10 MB). |
| Invoices | Upload an invoice; it is read into a draft, matched to an item, and recorded after you confirm it. See below. |
| Snooze / "I'm on it" | Owners can pause reminders from the dashboard or with a link in the email. Links are signed, expire, and stop working once the item is renewed. |
| Renewal progress | Mark quote requested, awaiting approval, approved or ordered; reminders pause until the expiry date. |
| Dashboard | Search, filter by status and category, sort, edit, renew, archive, import/export CSV, preview and send reminders, dark mode, phone layout. |

## How invoice reading works

1. You upload a PDF, image or text file (dashboard **Upload invoice**, or `thingsexpiretracker invoice add`).
2. The text is read: text-based PDFs and text files need nothing extra. Scans and photos need `tesseract` (and `pdftotext`
   for stubborn PDFs) on the PATH, **or** an Anthropic API key (`THINGSEXPIRE_ANTHROPIC_KEY`), in which case the file is sent
   to the Anthropic API and read there. That is opt-in and off by default; leave the key unset to keep every file local.
3. Fields are found (vendor, invoice number, dates, amount, currency) and the best-matching item is suggested from its
   reference, vendor or name. If nothing matches you choose the item.
4. You review a draft, with uncertain fields highlighted, and confirm. Only then is it recorded, optionally updating the
   item's cost and vendor.

Automatic reading is a convenience, not a guarantee: it handles ordinary invoice layouts, but unusual layouts, unusual fonts
or low-quality scans can be misread. That is why nothing is recorded without a person confirming it.

## Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `THINGSEXPIRE_DB` | database file (default `thingsexpiretracker.db`); upgraded automatically from older versions |
| `THINGSEXPIRE_TOKEN` | bearer token for the API; **required** to listen on anything but localhost |
| `THINGSEXPIRE_DEFAULT_RECIPIENT` | receives reminders for items with no owner email |
| `THINGSEXPIRE_ESCALATE_TO` | also told when an item is overdue for more than a week |
| `THINGSEXPIRE_WEBHOOK_URL` | incoming-webhook URL for the team channel |
| `THINGSEXPIRE_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_SSL` | email delivery; without a host, reminders print to the console |
| `THINGSEXPIRE_PUBLIC_URL` | the address people reach the dashboard at (for example `https://expiry.example.com`); with a secret it turns on "I'm on it" links in emails |
| `THINGSEXPIRE_SECRET` | signs those links (defaults to the API token) |
| `THINGSEXPIRE_FILES_DIR` | where attachments are stored (default `<database file>.files`) |
| `THINGSEXPIRE_ANTHROPIC_KEY` | optional: let Claude read invoices and scans (files are sent to the Anthropic API) |
| `THINGSEXPIRE_AI_MODEL` | model used for reading invoices (default `claude-sonnet-5-5`) |
| `THINGSEXPIRE_CURRENCY` | code shown with costs (default `USD`); amounts are not converted |
| `THINGSEXPIRE_APP_NAME` | title shown in the dashboard and messages |

## JSON API

All `/api` routes except `/api/health` need `Authorization: Bearer <token>` when a token is set.
The calendar feed also accepts `?token=` because calendar apps cannot send headers; treat that link like a password.

| Method and path | |
|---|---|
| `GET /api/items?q=&status=&category=&within_days=&archived=1` | list, most urgent first |
| `POST /api/items` | create (`name`, `expires_on` required; optional `vendor`, `cost`, `owner_email`, `lead_days`, ...) |
| `GET/PUT/DELETE /api/items/{id}` | read (with renewals, history, reminders) / update / archive |
| `POST /api/items/{id}/renew` | `{"months": 12, "note": "..."}` or `{"new_expires_on": "2027-01-31"}` |
| `POST /api/items/{id}/unarchive` | restore |
| `GET /api/summary` | counts, categories and `spend_cents` |
| `GET /api/activity?limit=30` | recent audit entries |
| `POST /api/remind` | run the reminder pass (`{"dry_run": true}` to preview) |
| `POST /api/import[?strict=1]` | body is CSV text; returns imported count and line-numbered errors |
| `GET /api/export.csv`, `GET /api/calendar.ics` | downloads |
| `POST /api/login`, `POST /api/logout`, `GET /api/me`, `GET /api/auth/status` | sign-in (returns a session token to send as the bearer token) |
| `GET/POST /api/users`, `PUT /api/users/{id}` | admin only: list, create, change role/password/disabled |
| `POST /api/items/{id}/snooze`, `POST /api/items/{id}/workflow` | `{"days": 7}` (0 clears) / `{"state": "approved"}` |
| `GET/POST /api/items/{id}/attachments?filename=` | list / upload (request body is the raw file) |
| `GET/DELETE /api/attachments/{id}` | download / remove |
| `POST /api/invoices/upload?filename=&item_id=` | raw file body; returns a draft, or `409` with what was read when no item matches |
| `GET /api/invoices?status=&item_id=`, `GET/PUT/DELETE /api/invoices/{id}`, `POST /api/invoices/{id}/confirm` | review and record (`{"update_cost": true}`) |
| `GET /ack?...` | the page behind an email "I'm on it" link (shows a button; the button snoozes) |
| `GET /api/health` | `{"status": "ok", "version": ...}` |

Roles are enforced on every route (viewer: read; editor: change; admin: users). Validation problems return `422` with `{"fields": {"expires_on": "..."}}`.

## Running it for a team

```bash
docker build -t thingsexpiretracker .
docker run -d -p 8080:8080 -v thingsexpiretracker-data:/data -e THINGSEXPIRE_TOKEN=change-me \
  -e THINGSEXPIRE_SMTP_HOST=smtp.example.com -e THINGSEXPIRE_SMTP_FROM=alerts@example.com thingsexpiretracker
# daily reminders (cron on the host):
0 8 * * *  docker exec <container> thingsexpiretracker remind
```

Put it behind HTTPS (a reverse proxy) before exposing it beyond a trusted network: the token travels in a header.

## Implementation notes

- `rules.py` calculates expiry status and reminder stages from explicit dates. It does not read the clock or access storage, which makes boundary cases straightforward to test.
- `store.py` handles item persistence and audit entries. Callers use its functions rather than repeating SQL in the CLI and HTTP handlers.
- Reminder deliveries are tracked by item, expiry date, stage and recipient. This avoids duplicate sends when a scheduled job runs again, while allowing failed deliveries to retry.
- Database migrations use SQLite's `user_version`; a database created with a newer schema version is rejected rather than opened with incompatible assumptions.
- Costs are stored as integer cents to avoid floating-point rounding issues. The displayed currency is a label, not a conversion service.
- Invoice extraction is a proposal for review, not an authoritative source. A user must confirm the draft before it is recorded.

### Operational limits

- The reminder job must be scheduled externally; the web server does not replace a scheduler.
- SQLite is suitable for a small deployment but is not a substitute for a shared database when multiple hosts need concurrent writes.
- A calendar URL containing a token should be handled like a credential. Use HTTPS and restrict access before exposing the application outside a trusted network.
- OCR and automated invoice extraction may miss fields or return incorrect values. Confirm the original document against the extracted draft.

## Tests

```bash
python -m unittest discover -s . -t . -v
```

## Licence

All rights reserved; see `LICENSE`.
