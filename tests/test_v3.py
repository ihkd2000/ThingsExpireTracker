import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

from thingsexpiretracker import attachments, auth, extract, invoices, links, reminders, store
from thingsexpiretracker.config import Settings
from thingsexpiretracker.notify import RecordingNotifier
from tests.helpers import DbCase
from tests.pdfmaker import make_pdf

INVOICE_LINES = ["Acme Hosting Ltd", "Invoice No: INV-2026-0042", "Invoice date: March 3, 2026", "Due date: 2026-04-02",
                 "Subtotal: $1,100.00", "Tax: $149.50", "Total: $1,249.50"]
INVOICE_TEXT = "\n".join(INVOICE_LINES)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


class AuthTests(DbCase):
    def test_password_hash_and_login(self):
        user = auth.create_user(self.conn, "sam", "correct horse battery", "editor")
        self.assertNotIn("password", " ".join(user))
        token, who = auth.login(self.conn, "SAM", "correct horse battery")
        self.assertEqual(who["role"], "editor")
        self.assertEqual(auth.user_for_token(self.conn, token)["username"], "sam")
        self.assertIsNone(auth.user_for_token(self.conn, token + "x"))
        with self.assertRaises(auth.AuthError):
            auth.login(self.conn, "sam", "wrong password!!")
        with self.assertRaises(auth.AuthError):
            auth.login(self.conn, "nobody", "whatever password")
        stored = self.conn.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertFalse(auth.verify_password("x", "garbage"))

    def test_validation_and_duplicates(self):
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.conn, "a b", "long enough password", "viewer")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.conn, "sam", "short", "viewer")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.conn, "sam", "long enough password", "boss")
        auth.create_user(self.conn, "sam", "long enough password", "viewer")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.conn, "SAM", "long enough password", "viewer")

    def test_last_admin_is_protected_and_disable_ends_sessions(self):
        admin = auth.create_user(self.conn, "root", "long enough password", "admin")
        with self.assertRaises(auth.AuthError):
            auth.update_user(self.conn, admin["id"], role="viewer")
        with self.assertRaises(auth.AuthError):
            auth.update_user(self.conn, admin["id"], disabled=True)
        other = auth.create_user(self.conn, "ed", "long enough password", "editor")
        token, _ = auth.login(self.conn, "ed", "long enough password")
        auth.update_user(self.conn, other["id"], disabled=True)
        self.assertIsNone(auth.user_for_token(self.conn, token))
        with self.assertRaises(auth.AuthError):
            auth.login(self.conn, "ed", "long enough password")

    def test_password_change_signs_out(self):
        user = auth.create_user(self.conn, "ed", "long enough password", "editor")
        token, _ = auth.login(self.conn, "ed", "long enough password")
        auth.update_user(self.conn, user["id"], password="another long password")
        self.assertIsNone(auth.user_for_token(self.conn, token))
        auth.login(self.conn, "ed", "another long password")

    def test_roles_order(self):
        self.assertTrue(auth.allows("admin", "editor"))
        self.assertTrue(auth.allows("editor", "viewer"))
        self.assertFalse(auth.allows("viewer", "editor"))

    def test_throttle(self):
        now = [0.0]
        t = auth.LoginThrottle(limit=3, window=60, clock=lambda: now[0])
        for _ in range(3):
            self.assertFalse(t.blocked("Sam"))
            t.failed("sam")
        self.assertTrue(t.blocked("SAM"))
        now[0] = 61
        self.assertFalse(t.blocked("sam"))


class AttachmentTests(DbCase):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self._dir.name, "files")
        self.item = store.create_item(self.conn, {"name": "Policy", "expires_on": "2030-01-01"})

    def test_save_read_delete(self):
        meta = attachments.save(self.conn, self.dir, self.item["id"], "cert.png", PNG, actor="sam")
        self.assertEqual((meta["content_type"], meta["size"]), ("image/png", len(PNG)))
        _, data = attachments.read(self.conn, self.dir, meta["id"])
        self.assertEqual(data, PNG)
        attachments.delete(self.conn, self.dir, meta["id"], "sam")
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual([h["action"] for h in store.history_for(self.conn, self.item["id"])][:2], ["detached", "attached"])

    def test_rejects_bad_files(self):
        for name, data in (("x.exe", b"MZ"), ("fake.pdf", b"not a pdf"), ("empty.txt", b""), ("bin.txt", b"\xff\xfe\x00"), ("a.png", b"GIF89a")):
            with self.assertRaises(attachments.AttachmentError, msg=name):
                attachments.save(self.conn, self.dir, self.item["id"], name, data)
        with self.assertRaises(attachments.AttachmentError):
            attachments.save(self.conn, self.dir, self.item["id"], "big.txt", b"a" * 20, max_bytes=10)
        with self.assertRaises(store.NotFound):
            attachments.save(self.conn, self.dir, 999, "a.txt", b"hi")

    def test_filename_cannot_escape_the_directory(self):
        self.assertEqual(attachments.clean_filename("../../etc/passwd"), "passwd")
        self.assertEqual(attachments.clean_filename("C:\\Users\\x\\inv?.pdf"), "inv.pdf")
        meta = attachments.save(self.conn, self.dir, self.item["id"], "../../evil.txt", b"hello")
        self.assertEqual(meta["filename"], "evil.txt")
        self.assertEqual(len(os.listdir(self.dir)), 1)
        self.assertNotIn("evil", os.listdir(self.dir)[0])


class ExtractTests(unittest.TestCase):
    def test_parse_standard_invoice(self):
        r = extract.parse_invoice_text(INVOICE_TEXT)
        f = r["fields"]
        self.assertEqual(f["vendor"], "Acme Hosting Ltd")
        self.assertEqual(f["invoice_number"], "INV-2026-0042")
        self.assertEqual(f["invoice_date"], "2026-03-03")
        self.assertEqual(f["due_date"], "2026-04-02")
        self.assertEqual((f["amount"], f["currency"]), ("1249.50", "USD"))
        self.assertEqual(r["confidence"]["amount"], "high")

    def test_amount_due_beats_total_and_european_numbers(self):
        r = extract.parse_invoice_text("Rechnung\nInvoice number: 77\nTotal 1.200,00 EUR\nAmount due: 900,50 EUR\n")
        self.assertEqual((r["fields"]["amount"], r["fields"]["currency"]), ("900.50", "EUR"))

    def test_ambiguous_dates(self):
        self.assertEqual(extract._to_iso("25/12/2026"), "2026-12-25")
        self.assertEqual(extract._to_iso("03/04/2026"), "2026-03-04")
        self.assertEqual(extract._to_iso("5th January 2027"), "2027-01-05")
        self.assertIsNone(extract._to_iso("31/31/2026"))

    def test_known_vendor_wins_and_empty_text_is_safe(self):
        r = extract.parse_invoice_text("Billing statement\nThanks for choosing Zed Registrar!\nTotal: £10.00", ["Zed Registrar"])
        self.assertEqual(r["fields"]["vendor"], "Zed Registrar")
        self.assertEqual(r["fields"]["currency"], "GBP")
        self.assertEqual(extract.parse_invoice_text("")["fields"], {})

    def test_builtin_pdf_reader(self):
        for compress in (True, False):
            text, method = extract.extract_text(make_pdf(INVOICE_LINES, compress), "i.pdf", "application/pdf")
            self.assertEqual(method, "pdf-builtin")
            self.assertIn("Invoice No: INV-2026-0042", text)
            self.assertIn("Total: $1,249.50", " ".join(text.split()))
        fields = extract.read_invoice(make_pdf(INVOICE_LINES), "i.pdf", "application/pdf", Settings())["fields"]
        self.assertEqual(fields["amount"], "1249.50")

    def test_unreadable_file_gives_a_helpful_note(self):
        result = extract.read_invoice(PNG, "scan.png", "image/png", Settings())
        self.assertEqual(result["fields"], {})
        self.assertTrue(result["notes"])


class _FakeAI(BaseHTTPRequestHandler):
    seen = []
    reply = {"content": [{"type": "text", "text": 'Here you go: {"vendor": "AI Vendor", "invoice_number": "A-1", "invoice_date": "2026-05-01",'
                                                   ' "due_date": "", "amount": "42.10", "currency": "eur"}'}]}
    status = 200

    def do_POST(self):
        _FakeAI.seen.append((self.headers.get("x-api-key"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(_FakeAI.status)
        self.end_headers()
        self.wfile.write(json.dumps(_FakeAI.reply).encode())

    def log_message(self, *a):
        pass


class ClaudeExtractTests(unittest.TestCase):
    def setUp(self):
        _FakeAI.seen, _FakeAI.status = [], 200
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeAI)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.settings = Settings(ai_key="k-123", ai_url="http://127.0.0.1:%d/v1/messages" % self.srv.server_address[1])

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_image_is_sent_and_fields_come_back(self):
        result = extract.read_invoice(PNG, "scan.png", "image/png", self.settings)
        self.assertEqual(result["source"], "claude")
        self.assertEqual(result["fields"]["vendor"], "AI Vendor")
        self.assertEqual(result["confidence"]["amount"], "ai")
        key, payload = _FakeAI.seen[0]
        self.assertEqual(key, "k-123")
        self.assertEqual(payload["messages"][0]["content"][0]["type"], "image")
        self.assertNotIn("due_date", result["fields"])

    def test_pdf_goes_as_document_and_merges_with_local_reading(self):
        result = extract.read_invoice(make_pdf(INVOICE_LINES), "i.pdf", "application/pdf", self.settings)
        self.assertEqual(result["source"], "claude+pdf-builtin")
        self.assertEqual(result["fields"]["due_date"], "2026-04-02")  # local reading fills what the AI left blank
        self.assertEqual(result["fields"]["vendor"], "AI Vendor")  # AI wins where it answered
        self.assertEqual(_FakeAI.seen[0][1]["messages"][0]["content"][0]["type"], "document")

    def test_failure_falls_back_with_a_note(self):
        _FakeAI.status = 500
        result = extract.read_invoice(make_pdf(INVOICE_LINES), "i.pdf", "application/pdf", self.settings)
        self.assertEqual(result["source"], "pdf-builtin")
        self.assertEqual(result["fields"]["amount"], "1249.50")
        self.assertIn("HTTP 500", result["notes"][0])


class InvoiceFlowTests(DbCase):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self._dir.name, "files")
        self.item = store.create_item(self.conn, {"name": "Hosting plan", "vendor": "Acme Hosting Ltd", "expires_on": "2030-01-01"})
        self.pdf = make_pdf(INVOICE_LINES)

    def test_ingest_suggests_item_then_confirm_records(self):
        draft = invoices.ingest(self.conn, self.dir, Settings(), "inv.pdf", self.pdf, actor="sam")
        self.assertEqual((draft["item_id"], draft["status"], draft["amount"]), (self.item["id"], "draft", "1249.50"))
        self.assertEqual(draft["suggested_item"], self.item["id"])
        done = invoices.confirm(self.conn, draft["id"], update_cost=True, vendor_to_item=True, actor="sam")
        self.assertEqual(done["status"], "confirmed")
        self.assertEqual(store.get_item(self.conn, self.item["id"])["cost"], "1249.50")
        self.assertEqual(invoices.total_for(self.conn, self.item["id"]), 124950)
        actions = [h["action"] for h in store.history_for(self.conn, self.item["id"])]
        self.assertIn("invoice", actions)
        self.assertIn("attached", actions)
        with self.assertRaises(store.ValidationError):
            invoices.update_draft(self.conn, draft["id"], {"amount": "1"})

    def test_unmatched_invoice_needs_an_item(self):
        text = b"Unknown Corp\nInvoice No: 9\nTotal: $5.00\n"
        with self.assertRaises(invoices.InvoiceNeedsItem) as ctx:
            invoices.ingest(self.conn, self.dir, Settings(), "x.txt", text)
        self.assertEqual(ctx.exception.result["fields"]["amount"], "5.00")
        self.assertFalse(os.path.exists(self.dir))  # nothing stored until we know where it belongs
        draft = invoices.ingest(self.conn, self.dir, Settings(), "x.txt", text, item_id=self.item["id"])
        self.assertIsNone(draft["suggested_item"])

    def test_review_edits_and_validation(self):
        draft = invoices.ingest(self.conn, self.dir, Settings(), "inv.pdf", self.pdf)
        with self.assertRaises(store.ValidationError) as ctx:
            invoices.update_draft(self.conn, draft["id"], {"amount": "lots", "due_date": "soon"})
        self.assertEqual(set(ctx.exception.errors), {"amount", "due_date"})
        fixed = invoices.update_draft(self.conn, draft["id"], {"amount": "1,000", "currency": "usd"})
        self.assertEqual((fixed["amount"], fixed["currency"]), ("1000.00", "USD"))
        blank = invoices.ingest(self.conn, self.dir, Settings(), "n.txt", b"nothing useful", item_id=self.item["id"])
        with self.assertRaises(store.ValidationError):
            invoices.confirm(self.conn, blank["id"])

    def test_discard_removes_draft_and_file(self):
        draft = invoices.ingest(self.conn, self.dir, Settings(), "inv.pdf", self.pdf)
        invoices.discard(self.conn, self.dir, draft["id"])
        self.assertEqual(invoices.list_invoices(self.conn), [])
        self.assertEqual(os.listdir(self.dir), [])


class SnoozeWorkflowTests(DbCase):
    def test_snooze_pauses_owner_but_not_escalation(self):
        item = store.create_item(self.conn, {"name": "A", "expires_on": "2026-06-01", "owner_email": "o@x.co"})
        store.snooze_item(self.conn, item["id"], 30, today=date(2026, 6, 8))
        mail = RecordingNotifier()
        reminders.run(self.conn, mail, date(2026, 6, 10), escalate_to="boss@x.co")
        self.assertEqual([m["to"] for m in mail.sent], ["boss@x.co"])
        reminders.run(self.conn, mail, date(2026, 7, 20), escalate_to="boss@x.co")
        self.assertIn("o@x.co", [m["to"] for m in mail.sent])

    def test_snooze_validation_and_clear(self):
        item = store.create_item(self.conn, {"name": "A", "expires_on": "2026-06-10"})
        for bad in (-1, 31, "7", True):
            with self.assertRaises(store.ValidationError):
                store.snooze_item(self.conn, item["id"], bad)
        self.assertEqual(store.snooze_item(self.conn, item["id"], 5, today=date(2026, 6, 1))["snoozed_until"], "2026-06-06")
        self.assertEqual(store.snooze_item(self.conn, item["id"], 0)["snoozed_until"], "")

    def test_workflow_pauses_before_expiry_only_and_renewal_resets(self):
        item = store.create_item(self.conn, {"name": "A", "expires_on": "2026-06-10", "owner_email": "o@x.co"})
        store.set_workflow(self.conn, item["id"], "awaiting_approval")
        mail = RecordingNotifier()
        reminders.run(self.conn, mail, date(2026, 6, 9))
        self.assertEqual(mail.sent, [])
        reminders.run(self.conn, mail, date(2026, 6, 11))  # expired: the pause ends
        self.assertEqual(len(mail.sent), 1)
        with self.assertRaises(store.ValidationError):
            store.set_workflow(self.conn, item["id"], "bogus")
        store.snooze_item(self.conn, item["id"], 3)
        renewed = store.renew_item(self.conn, item["id"], months=12, today=date(2026, 6, 11))
        self.assertEqual((renewed["workflow"], renewed["snoozed_until"]), ("", ""))


class LinkTests(unittest.TestCase):
    S = Settings(public_url="https://x.example", secret="s3cret")
    ITEM = {"id": 7, "expires_on": "2026-06-10"}

    def parse(self, url):
        from urllib.parse import parse_qs, urlparse
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        return int(q["d"]), int(q["x"]), q["s"]

    def test_sign_verify_expire(self):
        d, x, s = self.parse(links.snooze_link(self.S, self.ITEM, now=1000))
        self.assertTrue(links.verify(self.S, self.ITEM, d, x, s, now=2000))
        self.assertFalse(links.verify(self.S, self.ITEM, d, x, s, now=x + 1))
        self.assertFalse(links.verify(self.S, self.ITEM, d + 1, x, s, now=2000))
        self.assertFalse(links.verify(self.S, {"id": 8, "expires_on": "2026-06-10"}, d, x, s, now=2000))
        self.assertFalse(links.verify(self.S, {"id": 7, "expires_on": "2027-06-10"}, d, x, s, now=2000))  # renewed
        self.assertFalse(links.verify(Settings(public_url="https://x.example", secret="other"), self.ITEM, d, x, s, now=2000))

    def test_disabled_without_secret_or_url(self):
        self.assertFalse(links.enabled(Settings(public_url="https://x")))
        self.assertTrue(links.enabled(Settings(public_url="https://x", api_token="t")))

    def test_links_appear_in_owner_mail_only(self):
        with tempfile.TemporaryDirectory() as d:
            from thingsexpiretracker import db
            conn = db.connect(os.path.join(d, "l.db"))
            store.create_item(conn, {"name": "A", "expires_on": "2026-06-01", "owner_email": "o@x.co"})
            mail = RecordingNotifier()
            reminders.run(conn, mail, date(2026, 6, 10), escalate_to="boss@x.co", link_for=lambda i: links.snooze_link(self.S, i))
            by = {m["to"]: m for m in mail.sent}
            self.assertIn("/ack?", by["o@x.co"]["text"])
            self.assertIn("/ack?", by["o@x.co"]["html"])
            self.assertNotIn("/ack?", by["boss@x.co"]["text"])
            conn.close()


class MigrationV3Tests(unittest.TestCase):
    def test_v2_database_upgrades(self):
        from thingsexpiretracker import db
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v2.db")
            raw = sqlite3.connect(path)
            raw.executescript(db._SCHEMA_V1 + db._SCHEMA_V2)
            raw.execute("INSERT INTO items (name, expires_on, created_at) VALUES ('Old', '2030-01-01', 'x')")
            raw.execute("PRAGMA user_version = 2")
            raw.commit()
            raw.close()
            conn = db.connect(path)
            item = store.list_items(conn)[0]
            self.assertEqual((item["workflow"], item["snoozed_until"]), ("", ""))
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            conn.close()


if __name__ == "__main__":
    unittest.main()
