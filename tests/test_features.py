import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from thingsexpiretracker import csvio, db, ics, reminders, store
from thingsexpiretracker.notify import RecordingNotifier, WebhookNotifier
from tests.helpers import DbCase

TODAY = date(2026, 6, 1)


class CostTests(DbCase):
    def test_parse_cost(self):
        self.assertEqual(store.parse_cost("1,200.5"), 120050)
        self.assertEqual(store.parse_cost("0.005"), 1)
        self.assertIsNone(store.parse_cost(""))
        self.assertIsNone(store.parse_cost(None))
        for bad in ("abc", "-5", "NaN", "Infinity", True):
            with self.assertRaises(ValueError):
                store.parse_cost(bad)

    def test_cost_round_trip_and_validation(self):
        item = store.create_item(self.conn, {"name": "Domain", "expires_on": "2026-07-01", "cost": "99.9", "vendor": "Registrar"}, TODAY)
        self.assertEqual((item["cost"], item["vendor"]), ("99.90", "Registrar"))
        with self.assertRaises(store.ValidationError) as ctx:
            store.create_item(self.conn, {"name": "x", "expires_on": "2026-07-01", "cost": "lots"})
        self.assertIn("cost", ctx.exception.errors)
        updated = store.update_item(self.conn, item["id"], {"cost": ""}, TODAY)
        self.assertEqual(updated["cost"], "")

    def test_summary_spend(self):
        for name, day, cost in (("late", "2026-05-01", "10"), ("soon", "2026-06-20", "20"), ("mid", "2026-08-01", "30"),
                                ("far", "2027-03-01", "40"), ("free", "2026-06-05", "")):
            store.create_item(self.conn, {"name": name, "expires_on": day, "cost": cost}, TODAY)
        spend = store.summary(self.conn, TODAY)["spend_cents"]
        self.assertEqual(spend, {"overdue": 1000, "next_30": 2000, "next_90": 5000, "next_365": 9000})


class AuditTests(DbCase):
    def test_changes_are_recorded(self):
        item = store.create_item(self.conn, {"name": "A", "expires_on": "2026-07-01"}, TODAY, actor="cli")
        store.update_item(self.conn, item["id"], {"owner_email": "x@y.co", "notes": "n"}, TODAY, actor="api")
        store.update_item(self.conn, item["id"], {"name": "A"}, TODAY, actor="api")  # no change: nothing recorded
        store.renew_item(self.conn, item["id"], months=12, today=TODAY, actor="api")
        store.set_archived(self.conn, item["id"], True, actor="cli")
        actions = [(h["action"], h["actor"]) for h in reversed(store.history_for(self.conn, item["id"]))]
        self.assertEqual(actions, [("created", "cli"), ("updated", "api"), ("renewed", "api"), ("archived", "cli")])
        detail = store.history_for(self.conn, item["id"])[2]["detail"]
        self.assertIn("owner_email: - -> x@y.co", detail)
        self.assertIn("notes", detail)
        self.assertEqual(len(store.recent_activity(self.conn)), 4)

    def test_import_is_attributed_and_keeps_cost(self):
        csvio.import_csv(self.conn, io.StringIO("name,expires_on,cost,vendor\nLicence,2030-01-01,15.5,Acme\n"))
        item = store.list_items(self.conn)[0]
        self.assertEqual((item["cost"], item["vendor"]), ("15.50", "Acme"))
        self.assertEqual(store.history_for(self.conn, item["id"])[0]["actor"], "import")


class MigrationTests(unittest.TestCase):
    def test_v1_database_upgrades_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            raw = sqlite3.connect(path)
            raw.executescript(db._SCHEMA_V1)
            raw.execute("INSERT INTO items (name, expires_on, created_at) VALUES ('Old item', '2030-01-01', '2025-01-01 00:00:00')")
            raw.execute("PRAGMA user_version = 1")
            raw.commit()
            raw.close()
            conn = db.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                item = store.list_items(conn)[0]
                self.assertEqual((item["name"], item["cost"], item["vendor"]), ("Old item", "", ""))
                store.update_item(conn, item["id"], {"cost": "5"})
            finally:
                conn.close()

    def test_newer_database_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "new.db")
            raw = sqlite3.connect(path)
            raw.execute("PRAGMA user_version = 99")
            raw.commit()
            raw.close()
            with self.assertRaises(RuntimeError):
                db.connect(path)


class IcsTests(DbCase):
    def test_calendar_structure(self):
        store.create_item(self.conn, {"name": "Cert, with; chars", "expires_on": "2026-07-01", "reference": "R1",
                                      "lead_days": "14,7"}, TODAY)
        text = ics.build_calendar(store.list_items(self.conn, today=TODAY), "Test", now=datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertIn("DTSTART;VALUE=DATE:20260701", text)
        self.assertIn("DTEND;VALUE=DATE:20260702", text)
        self.assertIn("SUMMARY:Expires: Cert" + chr(92) + ", with" + chr(92) + "; chars", text)
        self.assertEqual(text.count("BEGIN:VALARM"), 2)
        self.assertIn("TRIGGER:-P14D", text)
        self.assertIn("UID:item-1-20260701@thingsexpiretracker", text)

    def test_long_lines_fold_without_splitting_characters(self):
        store.create_item(self.conn, {"name": "é" * 120, "expires_on": "2026-07-01"}, TODAY)
        text = ics.build_calendar(store.list_items(self.conn, today=TODAY))
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        unfolded = text.replace("\r\n ", "")
        self.assertIn("SUMMARY:Expires: " + "é" * 120, unfolded)


class _Hook(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.received.append(json.loads(body))
        self.send_response(_Hook.status)
        self.end_headers()

    def log_message(self, *a):
        pass


class WebhookTests(DbCase):
    def setUp(self):
        super().setUp()
        _Hook.received, _Hook.status = [], 200
        self.srv = HTTPServer(("127.0.0.1", 0), _Hook)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hook" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_notifier_posts_json_and_reports_http_errors(self):
        WebhookNotifier(self.url).send(to="webhook", subject="S", text="body", html="")
        self.assertEqual(_Hook.received[0]["text"], "*S*\nbody")
        _Hook.status = 500
        with self.assertRaises(RuntimeError):
            WebhookNotifier(self.url).send(to="webhook", subject="S", text="b", html="")
        with self.assertRaises(ValueError):
            WebhookNotifier("ftp://nope")

    def test_orphan_items_go_to_the_channel_once(self):
        store.create_item(self.conn, {"name": "Orphan", "expires_on": "2026-06-10"})
        mail = RecordingNotifier()
        hook = WebhookNotifier(self.url)
        r = reminders.run(self.conn, mail, date(2026, 6, 9), webhook=hook)
        self.assertEqual((r.webhook_sent, r.no_recipient, mail.sent), (1, [], []))
        reminders.run(self.conn, mail, date(2026, 6, 9), webhook=hook)
        self.assertEqual(len(_Hook.received), 1)

    def test_owned_items_get_email_plus_channel_copy(self):
        store.create_item(self.conn, {"name": "Owned", "expires_on": "2026-06-10", "owner_email": "a@b.co"})
        mail = RecordingNotifier()
        r = reminders.run(self.conn, mail, date(2026, 6, 9), webhook=WebhookNotifier(self.url))
        self.assertEqual((r.sent, r.webhook_sent), (1, 1))
        self.assertEqual(len(_Hook.received), 1)

    def test_webhook_failure_does_not_block_email(self):
        _Hook.status = 500
        store.create_item(self.conn, {"name": "Owned", "expires_on": "2026-06-10", "owner_email": "a@b.co"})
        mail = RecordingNotifier()
        r = reminders.run(self.conn, mail, date(2026, 6, 9), webhook=WebhookNotifier(self.url))
        self.assertEqual((r.sent, r.webhook_failed, r.failed), (1, 1, 0))


class EscalationTests(DbCase):
    def test_boss_hears_after_a_week_overdue_only(self):
        store.create_item(self.conn, {"name": "Lapsed", "expires_on": "2026-06-01", "owner_email": "o@x.co"})
        mail = RecordingNotifier()
        reminders.run(self.conn, mail, date(2026, 6, 3), escalate_to="boss@x.co")   # week 1: owner only
        self.assertEqual([m["to"] for m in mail.sent], ["o@x.co"])
        reminders.run(self.conn, mail, date(2026, 6, 10), escalate_to="boss@x.co")  # week 2: owner + boss
        self.assertEqual(sorted(m["to"] for m in mail.sent), ["boss@x.co", "o@x.co", "o@x.co"])
        boss = [m for m in mail.sent if m["to"] == "boss@x.co"][0]
        self.assertIn("escalation", boss["subject"])
        reminders.run(self.conn, mail, date(2026, 6, 11), escalate_to="boss@x.co")  # same week: nothing new
        self.assertEqual(len(mail.sent), 3)

    def test_no_double_mail_when_owner_is_the_escalation_contact(self):
        store.create_item(self.conn, {"name": "Lapsed", "expires_on": "2026-06-01", "owner_email": "boss@x.co"})
        mail = RecordingNotifier()
        reminders.run(self.conn, mail, date(2026, 6, 10), escalate_to="Boss@x.co")
        self.assertEqual(len(mail.sent), 1)

    def test_changing_owner_notifies_the_new_owner(self):
        item = store.create_item(self.conn, {"name": "A", "expires_on": "2026-06-10", "owner_email": "old@x.co"})
        mail = RecordingNotifier()
        reminders.run(self.conn, mail, date(2026, 6, 9))
        store.update_item(self.conn, item["id"], {"owner_email": "new@x.co"})
        reminders.run(self.conn, mail, date(2026, 6, 9))
        self.assertEqual([m["to"] for m in mail.sent], ["old@x.co", "new@x.co"])

    def test_message_includes_cost_and_vendor(self):
        store.create_item(self.conn, {"name": "A", "expires_on": "2026-06-10", "owner_email": "o@x.co", "vendor": "Acme", "cost": "120"})
        mail = RecordingNotifier()
        reminders.run(self.conn, mail, date(2026, 6, 9), currency="EUR")
        self.assertIn("Acme", mail.sent[0]["text"])
        self.assertIn("120.00 EUR", mail.sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
