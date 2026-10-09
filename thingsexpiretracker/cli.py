"""Command line interface. main() accepts its streams and notifier so the tests can call it directly."""
from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import sys
from datetime import date
from typing import Callable, Optional, Sequence, TextIO

from . import __version__, attachments, auth, csvio, ics, invoices, links, reminders, rules, store
from .config import Settings
from .db import connect
from .notify import Notifier, get_notifier, get_webhook


def _parse_day(text: str) -> date:
    try:
        return store.parse_date(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="thingsexpiretracker", description="Track anything that expires and get reminded in time.")
    p.add_argument("--db", help="database file (default: $THINGSEXPIRE_DB or ./thingsexpiretracker.db)")
    p.add_argument("--version", action="version", version=f"thingsexpiretracker {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add an item")
    a.add_argument("name")
    a.add_argument("expires_on", type=_parse_day, metavar="YYYY-MM-DD")
    a.add_argument("--category", default="General")
    a.add_argument("--owner", default="", help="owner's name")
    a.add_argument("--email", default="", help="owner's email; reminders go here")
    a.add_argument("--reference", default="", help="licence or policy number")
    a.add_argument("--lead-days", default="", help="reminder days before expiry, e.g. 30,14,7,1")
    a.add_argument("--vendor", default="", help="who you buy it from")
    a.add_argument("--cost", default="", help="renewal cost, e.g. 1200 or 1200.50")
    a.add_argument("--notes", default="")

    ls = sub.add_parser("list", help="list items, most urgent first")
    ls.add_argument("--within", type=int, metavar="DAYS", help="only items expiring within DAYS (or already expired)")
    ls.add_argument("--category", default="")
    ls.add_argument("--status", choices=list(rules.STATUS_LABELS), default="")
    ls.add_argument("--search", default="")
    ls.add_argument("--all", action="store_true", help="include archived items")
    ls.add_argument("--json", action="store_true", help="machine-readable output")

    sh = sub.add_parser("show", help="show one item and its history")
    sh.add_argument("id", type=int)
    sh.add_argument("--json", action="store_true", help="machine-readable output")

    r = sub.add_parser("renew", help="record a renewal")
    r.add_argument("id", type=int)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--to", type=_parse_day, metavar="YYYY-MM-DD", help="the new expiry date")
    g.add_argument("--months", type=int, help="extend by this many months")
    r.add_argument("--note", default="")

    ar = sub.add_parser("archive", help="stop tracking an item (kept in the database)")
    ar.add_argument("id", type=int)
    un = sub.add_parser("unarchive", help="bring an archived item back")
    un.add_argument("id", type=int)

    rem = sub.add_parser("remind", help="send the reminders that are due (run this daily)")
    rem.add_argument("--dry-run", action="store_true", help="show what would be sent")
    rem.add_argument("--today", type=_parse_day, help="pretend today is this date")

    im = sub.add_parser("import", help="import items from a CSV file")
    im.add_argument("file")
    im.add_argument("--strict", action="store_true", help="import nothing if any row has a problem")
    ex = sub.add_parser("export", help="write all items as CSV")
    ex.add_argument("file", nargs="?", help="output file (default: standard output)")

    sm = sub.add_parser("summary", help="counts per status and the money coming due")
    sm.add_argument("--json", action="store_true", help="machine-readable output")
    hi = sub.add_parser("history", help="audit trail: for one item, or recent changes across all items")
    hi.add_argument("id", type=int, nargs="?")
    hi.add_argument("--limit", type=int, default=30)
    ic = sub.add_parser("ics", help="write a calendar file (.ics) you can subscribe to or import")
    ic.add_argument("file", nargs="?", help="output file (default: standard output)")
    bk = sub.add_parser("backup", help="write a consistent copy of the database")
    bk.add_argument("file")

    sn = sub.add_parser("snooze", help="pause owner reminders for an item (0 clears the pause)")
    sn.add_argument("id", type=int)
    sn.add_argument("days", type=int)
    wf = sub.add_parser("workflow", help="mark a renewal as in progress")
    wf.add_argument("id", type=int)
    wf.add_argument("state", choices=[w for w in store.WORKFLOWS if w] + ["none"])

    at = sub.add_parser("attach", help="attach a file (certificate, policy, ...) to an item")
    at.add_argument("id", type=int)
    at.add_argument("file")
    ats = sub.add_parser("attachments", help="list an item's files, or save one with --save ID")
    ats.add_argument("id", type=int)
    ats.add_argument("--save", type=int, metavar="ATTACHMENT_ID")
    ats.add_argument("--to", default="", help="where to save it (default: its original name)")

    inv = sub.add_parser("invoice", help="read invoices into drafts and record them")
    isub = inv.add_subparsers(dest="invoice_command", required=True)
    ia = isub.add_parser("add", help="upload an invoice; it is read into a draft for you to confirm")
    ia.add_argument("file")
    ia.add_argument("--item", type=int, help="item it belongs to (default: best match)")
    il = isub.add_parser("list", help="list invoices")
    il.add_argument("--status", choices=list(invoices.STATUSES), default="")
    il.add_argument("--item", type=int)
    ic = isub.add_parser("confirm", help="record a draft (fix fields with the options first)")
    ic.add_argument("id", type=int)
    ic.add_argument("--vendor")
    ic.add_argument("--number")
    ic.add_argument("--amount")
    ic.add_argument("--currency")
    ic.add_argument("--date", help="invoice date YYYY-MM-DD")
    ic.add_argument("--due", help="due date YYYY-MM-DD")
    ic.add_argument("--item", type=int)
    ic.add_argument("--update-cost", action="store_true", help="also set the item's renewal cost to this amount")
    ic.add_argument("--update-vendor", action="store_true", help="also set the item's vendor")
    idl = isub.add_parser("discard", help="throw a draft away")
    idl.add_argument("id", type=int)

    us = sub.add_parser("user", help="manage dashboard users (admin, editor, viewer)")
    usub = us.add_subparsers(dest="user_command", required=True)
    ua = usub.add_parser("add")
    ua.add_argument("username")
    ua.add_argument("--role", choices=list(auth.ROLES), default="viewer")
    ua.add_argument("--password", help="omit to be asked (recommended)")
    usub.add_parser("list")
    up = usub.add_parser("passwd")
    up.add_argument("username")
    up.add_argument("--password")
    ur = usub.add_parser("role")
    ur.add_argument("username")
    ur.add_argument("role", choices=list(auth.ROLES))
    ud = usub.add_parser("disable")
    ud.add_argument("username")
    ue = usub.add_parser("enable")
    ue.add_argument("username")

    sv = sub.add_parser("serve", help="run the web dashboard and API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    return p


def _row(item: dict) -> str:
    who = item["owner_name"] or item["owner_email"] or "-"
    return (
        f"{item['id']:>4}  {item['expires_on']}  {item['days_left']:>5}d  {item['status_label']:<9}  "
        f"{item['name'][:34]:<34}  {item['category'][:14]:<14}  {who[:22]}"
    )


def _webhook(settings: Settings, err: TextIO):
    try:
        return get_webhook(settings)
    except ValueError as exc:
        print(f"warning: {exc} Webhook disabled.", file=err)
        return None


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
    settings: Optional[Settings] = None,
    notifier: Optional[Notifier] = None,
    serve: Optional[Callable[..., None]] = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    settings = settings or Settings.from_env()
    db_path = args.db or settings.db_path

    try:
        conn = connect(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open the database '{db_path}': {exc}", file=err)
        return 2

    try:
        return _run(args, conn, settings, notifier, serve, out, err, db_path)
    except store.ValidationError as exc:
        for fld, message in exc.errors.items():
            print(f"error: {fld}: {message}", file=err)
        return 1
    except store.NotFound as exc:
        print(f"error: {exc}", file=err)
        return 1
    finally:
        conn.close()


def _run(args, conn, settings, notifier, serve, out, err, db_path) -> int:
    cmd = args.command
    today = date.today()

    if cmd == "add":
        item = store.create_item(conn, {
            "name": args.name, "expires_on": args.expires_on.isoformat(), "category": args.category,
            "owner_name": args.owner, "owner_email": args.email, "reference": args.reference,
            "lead_days": args.lead_days, "notes": args.notes,
            "vendor": args.vendor, "cost": args.cost,
        }, actor="cli")
        print(f"Added #{item['id']} {item['name']} (expires {item['expires_on']}, {item['status_label']}).", file=out)
        return 0

    if cmd == "list":
        items = store.list_items(conn, query=args.search, category=args.category, status=args.status,
                                 include_archived=args.all, within_days=args.within)
        if args.json:
            print(json.dumps(items, indent=2), file=out)
            return 0
        if not items:
            print("No items.", file=out)
            return 0
        print(f"{'ID':>4}  {'Expires':<10}  {'Left':>6}  {'Status':<9}  {'Name':<34}  {'Category':<14}  Owner", file=out)
        for item in items:
            print(_row(item), file=out)
        return 0

    if cmd == "show":
        item = store.get_item(conn, args.id)
        if args.json:
            item["renewals"] = store.renewals_for(conn, args.id)
            item["history"] = store.history_for(conn, args.id)
            print(json.dumps(item, indent=2), file=out)
            return 0
        print(f"#{item['id']} {item['name']}{'  [archived]' if item['archived'] else ''}", file=out)
        for label, key in (("Category", "category"), ("Vendor", "vendor"), ("Cost", "cost"), ("Owner", "owner_name"), ("Email", "owner_email"),
                           ("Reference", "reference"), ("Expires", "expires_on"), ("Notes", "notes")):
            print(f"  {label:<10} {item[key] or '-'}", file=out)
        print(f"  {'Status':<10} {item['status_label']} ({item['days_left']} days)", file=out)
        print(f"  {'Reminders':<10} {', '.join(str(n) for n in item['lead_days'])} days before", file=out)
        history = store.renewals_for(conn, args.id)
        if history:
            print("  Renewals:", file=out)
            for h in history:
                note = f" - {h['note']}" if h["note"] else ""
                print(f"    {h['renewed_at'][:10]}: {h['old_expires_on']} -> {h['new_expires_on']}{note}", file=out)
        return 0

    if cmd == "renew":
        item = store.renew_item(conn, args.id, new_expires_on=args.to.isoformat() if args.to else None,
                                months=args.months, note=args.note, actor="cli")
        print(f"Renewed #{item['id']} {item['name']}: now expires {item['expires_on']}.", file=out)
        return 0

    if cmd in ("archive", "unarchive"):
        store.set_archived(conn, args.id, cmd == "archive", actor="cli")
        print(f"{'Archived' if cmd == 'archive' else 'Restored'} #{args.id}.", file=out)
        return 0

    if cmd == "remind":
        day = args.today or today
        if args.dry_run:
            report = reminders.run(conn, notifier or get_notifier(settings), day,
                                   default_recipient=settings.default_recipient, app_name=settings.app_name, dry_run=True,
                                   escalate_to=settings.escalate_to, webhook=_webhook(settings, err), currency=settings.currency)
            if not report.planned:
                print("Nothing to send.", file=out)
            for p in report.planned:
                target = f"email {p.recipient}" if p.recipient else "post to the webhook"
                tag = " [escalation]" if p.escalation else ""
                print(f"Would {target}: {p.item['name']} ({rules.stage_label(p.stage)}){tag}", file=out)
        else:
            try:
                chosen = notifier or get_notifier(settings)
            except ValueError as exc:
                print(f"error: {exc}", file=err)
                return 2
            report = reminders.run(conn, chosen, day, default_recipient=settings.default_recipient,
                                   app_name=settings.app_name, escalate_to=settings.escalate_to,
                                   webhook=_webhook(settings, err), currency=settings.currency,
                                   link_for=(lambda item: links.snooze_link(settings, item)) if links.enabled(settings) else None)
            print(f"Sent {report.sent} email(s) covering {report.items_notified} item(s); {report.failed} failed.", file=out)
            if report.webhook_sent or report.webhook_failed:
                print(f"Webhook: {report.webhook_sent} posted, {report.webhook_failed} failed.", file=out)
        for name in report.no_recipient:
            print(f"warning: '{name}' is due but has no owner email and no default recipient is set.", file=err)
        return 1 if (report.failed or report.webhook_failed) else 0

    if cmd == "import":
        try:
            with open(args.file, newline="", encoding="utf-8-sig") as handle:
                result = csvio.import_csv(conn, handle, strict=args.strict)
        except OSError as exc:
            print(f"error: could not read '{args.file}': {exc}", file=err)
            return 2
        print(f"Imported {result.imported}; skipped {result.skipped_duplicates} duplicate(s); {len(result.errors)} problem(s).", file=out)
        for line in result.errors:
            print(f"  {line}", file=err)
        return 1 if result.errors else 0

    if cmd == "export":
        text = csvio.export_csv(store.list_items(conn, include_archived=True))
        if args.file:
            with open(args.file, "w", newline="", encoding="utf-8") as handle:
                handle.write(text)
            print(f"Wrote {args.file}.", file=out)
        else:
            out.write(text)
        return 0

    if cmd == "summary":
        data = store.summary(conn)
        if args.json:
            print(json.dumps(data, indent=2), file=out)
            return 0
        c = data["counts"]
        print(f"Tracking {data['total']} item(s): {c['expired']} expired, {c['due_today']} due today, "
              f"{c['due_soon']} due soon, {c['ok']} ok.", file=out)
        spend = data["spend_cents"]
        if any(spend.values()):
            money = lambda cents: f"{cents // 100:,}.{cents % 100:02d} {settings.currency}"  # noqa: E731
            print(f"Renewal cost: overdue {money(spend['overdue'])}; next 30 days {money(spend['next_30'])}; "
                  f"next 90 days {money(spend['next_90'])}; next year {money(spend['next_365'])}.", file=out)
        return 0

    if cmd == "history":
        if args.id is not None:
            store.get_item(conn, args.id)
            rows = store.history_for(conn, args.id)
        else:
            rows = store.recent_activity(conn, args.limit)
        if not rows:
            print("No history yet.", file=out)
        for r in rows:
            name = f"  #{r['item_id']} {r['item_name']}" if "item_name" in r else ""
            print(f"{r['at']}  {r['actor']:<7} {r['action']:<9} {r['detail']}{name}", file=out)
        return 0

    if cmd == "ics":
        text = ics.build_calendar(store.list_items(conn), settings.app_name)
        if args.file:
            with open(args.file, "w", newline="", encoding="utf-8") as handle:
                handle.write(text)
            print(f"Wrote {args.file}.", file=out)
        else:
            out.write(text)
        return 0

    if cmd == "backup":
        target = sqlite3.connect(args.file)
        try:
            conn.backup(target)
        finally:
            target.close()
        print(f"Backed up to {args.file}.", file=out)
        return 0

    if cmd == "snooze":
        item = store.snooze_item(conn, args.id, args.days, actor="cli")
        print(f"Reminders for #{item['id']} {item['name']} " + (f"are paused until {item['snoozed_until']}." if item["snoozed_until"] else "resume."), file=out)
        return 0

    if cmd == "workflow":
        item = store.set_workflow(conn, args.id, "" if args.state == "none" else args.state, actor="cli")
        print(f"#{item['id']} {item['name']}: renewal status is {item['workflow'] or 'not started'}.", file=out)
        return 0

    files = attachments.files_dir(db_path, settings.attachments_dir)

    if cmd == "attach":
        try:
            with open(args.file, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            print(f"error: could not read '{args.file}': {exc}", file=err)
            return 2
        try:
            meta = attachments.save(conn, files, args.id, args.file, data, actor="cli")
        except attachments.AttachmentError as exc:
            print(f"error: {exc}", file=err)
            return 1
        print(f"Attached {meta['filename']} ({meta['size']} bytes) to #{args.id} as file {meta['id']}.", file=out)
        return 0

    if cmd == "attachments":
        store.get_item(conn, args.id)
        if args.save:
            meta, data = attachments.read(conn, files, args.save)
            target = args.to or meta["filename"]
            with open(target, "wb") as handle:
                handle.write(data)
            print(f"Saved {target}.", file=out)
            return 0
        rows = attachments.list_for(conn, args.id)
        if not rows:
            print("No files.", file=out)
        for r in rows:
            print(f"{r['id']:>4}  {r['uploaded_at'][:16]}  {r['kind']:<8} {r['size']:>9}  {r['filename']}", file=out)
        return 0

    if cmd == "invoice":
        return _invoice(args, conn, settings, files, out, err)

    if cmd == "user":
        return _user(args, conn, out, err)

    if cmd == "serve":
        from .web import serve as default_serve

        runner = serve or default_serve
        try:
            runner(settings, db_path, args.host, args.port, out=out)
        except ValueError as exc:
            print(f"error: {exc}", file=err)
            return 2
        return 0

    return 2  # pragma: no cover - argparse enforces the command


def _invoice(args, conn, settings, files, out, err) -> int:
    sub = args.invoice_command
    if sub == "add":
        try:
            with open(args.file, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            print(f"error: could not read '{args.file}': {exc}", file=err)
            return 2
        try:
            draft = invoices.ingest(conn, files, settings, args.file, data, item_id=args.item, actor="cli")
        except invoices.InvoiceNeedsItem as exc:
            print("error: could not tell which item this invoice belongs to; run again with --item ID.", file=err)
            for key, value in exc.result["fields"].items():
                print(f"  read {key}: {value}", file=err)
            return 1
        except attachments.AttachmentError as exc:
            print(f"error: {exc}", file=err)
            return 1
        print(f"Draft invoice #{draft['id']} for item #{draft['item_id']} (read by: {draft['source'] or 'nothing'}).", file=out)
        for key in ("vendor", "invoice_number", "invoice_date", "due_date", "amount", "currency"):
            print(f"  {key:<15} {draft[key] or '-'}", file=out)
        for note in draft.get("notes", []):
            print(f"  note: {note}", file=out)
        print(f"Check it, then: thingsexpiretracker invoice confirm {draft['id']} [--amount ...] [--update-cost]", file=out)
        return 0
    if sub == "list":
        rows = invoices.list_invoices(conn, item_id=args.item, status=args.status)
        if not rows:
            print("No invoices.", file=out)
        for r in rows:
            print(f"{r['id']:>4}  {r['status']:<9} item {r['item_id'] or '-':<4} {r['invoice_date'] or '-':<10} "
                  f"{r['amount'] or '-':>10} {r['currency']:<4} {r['invoice_number'] or '-':<14} {r['vendor']}", file=out)
        return 0
    if sub == "confirm":
        edits = {"vendor": args.vendor, "invoice_number": args.number, "amount": args.amount, "currency": args.currency,
                 "invoice_date": args.date, "due_date": args.due, "item_id": args.item}
        edits = {k: v for k, v in edits.items() if v is not None}
        if edits:
            invoices.update_draft(conn, args.id, edits)
        done = invoices.confirm(conn, args.id, update_cost=args.update_cost, vendor_to_item=args.update_vendor, actor="cli")
        print(f"Recorded invoice #{done['id']}: {done['amount']} {done['currency']} for item #{done['item_id']}.", file=out)
        return 0
    invoices.discard(conn, files, args.id, "cli")
    print(f"Discarded draft #{args.id}.", file=out)
    return 0


def _user(args, conn, out, err) -> int:
    sub = args.user_command
    try:
        if sub == "list":
            for u in auth.list_users(conn):
                print(f"{u['username']:<20} {u['role']:<8} {'disabled' if u['disabled'] else 'active'}", file=out)
            return 0
        if sub == "add":
            password = args.password or getpass.getpass("Password: ")
            user = auth.create_user(conn, args.username, password, args.role)
            print(f"Created {user['role']} '{user['username']}'.", file=out)
            return 0
        row = conn.execute("SELECT id FROM users WHERE username = ?", (args.username,)).fetchone()
        if row is None:
            print(f"error: no user named '{args.username}'.", file=err)
            return 1
        if sub == "passwd":
            auth.update_user(conn, row["id"], password=args.password or getpass.getpass("New password: "))
            print("Password changed; the user was signed out.", file=out)
        elif sub == "role":
            auth.update_user(conn, row["id"], role=args.role)
            print(f"'{args.username}' is now {args.role}.", file=out)
        else:
            auth.update_user(conn, row["id"], disabled=(sub == "disable"))
            print(f"'{args.username}' is {'disabled' if sub == 'disable' else 'enabled'}.", file=out)
        return 0
    except auth.AuthError as exc:
        print(f"error: {exc}", file=err)
        return 1
