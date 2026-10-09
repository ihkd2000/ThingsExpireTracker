import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import urlparse

from thingsexpiretracker import auth, db, links, store
from thingsexpiretracker.config import Settings
from thingsexpiretracker.notify import RecordingNotifier
from thingsexpiretracker.web import make_server
from tests.pdfmaker import make_pdf
from tests.test_v3 import INVOICE_LINES, PNG


class Base(unittest.TestCase):
    settings = Settings()

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls._dir.name, "w.db")
        cls.mail = RecordingNotifier()
        cls.server = make_server(cls.settings, cls.path, "127.0.0.1", 0, notifier=cls.mail)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._dir.cleanup()

    def call(self, method, path, body=None, token=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", ctype)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                kind = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(payload) if "json" in kind else payload), resp.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            kind = exc.headers.get("Content-Type", "")
            return exc.code, (json.loads(payload) if "json" in kind else payload), exc.headers


class RolesTests(Base):
    PW = "long enough password"

    def test_everything(self):
        self.assertFalse(self.call("GET", "/api/auth/status")[1]["login_required"])
        conn = db.connect(self.path)
        for name, role in (("root", "admin"), ("ed", "editor"), ("vi", "viewer")):
            auth.create_user(conn, name, self.PW, role)
        conn.close()
        self.assertTrue(self.call("GET", "/api/auth/status")[1]["login_required"])
        self.assertEqual(self.call("GET", "/api/items")[0], 401)  # open install closes once users exist

        def login(name, pw=None):
            return self.call("POST", "/api/login", {"username": name, "password": pw or self.PW})

        self.assertEqual(login("ed", "wrong password!!")[0], 401)
        tokens = {n: login(n)[1]["token"] for n in ("root", "ed", "vi")}

        # viewer: read only
        self.assertEqual(self.call("GET", "/api/items", token=tokens["vi"])[0], 200)
        self.assertEqual(self.call("POST", "/api/items", {"name": "x", "expires_on": "2030-01-01"}, tokens["vi"])[0], 403)
        self.assertEqual(self.call("GET", "/api/users", token=tokens["vi"])[0], 403)
        # editor: edits, no user admin; the audit trail names the person
        status, item, _ = self.call("POST", "/api/items", {"name": "Policy", "expires_on": "2030-01-01"}, tokens["ed"])
        self.assertEqual(status, 201)
        history = self.call("GET", "/api/items/%d" % item["id"], token=tokens["ed"])[1]["history"]
        self.assertEqual(history[0]["actor"], "ed")
        self.assertEqual(self.call("POST", "/api/users", {"username": "n", "password": self.PW}, tokens["ed"])[0], 403)
        self.assertEqual(self.call("GET", "/api/me", token=tokens["ed"])[1]["role"], "editor")
        # admin manages users
        status, created, _ = self.call("POST", "/api/users", {"username": "new", "password": self.PW, "role": "viewer"}, tokens["root"])
        self.assertEqual(status, 201)
        self.assertNotIn("password_hash", created)
        self.assertEqual(self.call("POST", "/api/users", {"username": "weak", "password": "short"}, tokens["root"])[0], 422)
        self.assertEqual(self.call("PUT", "/api/users/%d" % created["id"], {"disabled": True}, tokens["root"])[0], 200)
        self.assertEqual(login("new")[0], 401)
        # logout kills the session
        self.assertEqual(self.call("POST", "/api/logout", {}, tokens["vi"])[0], 200)
        self.assertEqual(self.call("GET", "/api/items", token=tokens["vi"])[0], 401)
        # brute force is slowed down
        codes = [login("root", "bad password!!")[0] for _ in range(7)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(login("root")[0], 429)  # even the right password waits out the lock


class TokenAndFilesTests(Base):
    settings = Settings(api_token="tok", secret="sig-secret", public_url="http://127.0.0.1")

    def test_attachments_and_invoice_flow(self):
        status, item, _ = self.call("POST", "/api/items", {"name": "Hosting plan", "vendor": "Acme Hosting Ltd", "expires_on": "2030-01-01"}, "tok")
        iid = item["id"]
        # plain attachment
        status, meta, _ = self.call("POST", "/api/items/%d/attachments?filename=cert.png" % iid, raw=PNG, token="tok", ctype="application/octet-stream")
        self.assertEqual(status, 201)
        status, data, headers = self.call("GET", "/api/attachments/%d" % meta["id"], token="tok")
        self.assertEqual((status, data), (200, PNG))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.call("POST", "/api/items/%d/attachments?filename=a.exe" % iid, raw=b"MZ", token="tok")[0], 422)
        self.assertEqual(self.call("GET", "/api/attachments/%d" % meta["id"])[0], 401)
        # invoice: upload -> draft -> edit -> confirm
        pdf = make_pdf(INVOICE_LINES)
        status, draft, _ = self.call("POST", "/api/invoices/upload?filename=inv.pdf", raw=pdf, token="tok")
        self.assertEqual((status, draft["item_id"], draft["amount"], draft["source"]), (201, iid, "1249.50", "pdf-builtin"))
        self.assertEqual(self.call("PUT", "/api/invoices/%d" % draft["id"], {"amount": "nope"}, "tok")[0], 422)
        status, done, _ = self.call("POST", "/api/invoices/%d/confirm" % draft["id"], {"update_cost": True}, "tok")
        self.assertEqual((status, done["status"]), (200, "confirmed"))
        detail = self.call("GET", "/api/items/%d" % iid, token="tok")[1]
        self.assertEqual(detail["cost"], "1249.50")
        self.assertEqual(len(detail["invoices"]), 1)
        self.assertEqual(len(detail["attachments"]), 2)
        # no match -> 409 with what was read, then retry naming the item
        stray = b"Somebody Else\nInvoice No: 5\nTotal: $9.00\n"
        status, body, _ = self.call("POST", "/api/invoices/upload?filename=s.txt", raw=stray, token="tok")
        self.assertEqual((status, body["needs_item"], body["fields"]["amount"]), (409, True, "9.00"))
        self.assertEqual(self.call("POST", "/api/invoices/upload?filename=s.txt&item_id=%d" % iid, raw=stray, token="tok")[0], 201)
        self.assertEqual(len(self.call("GET", "/api/invoices?status=draft", token="tok")[1]["invoices"]), 1)
        # discard
        draft2 = self.call("GET", "/api/invoices?status=draft", token="tok")[1]["invoices"][0]
        self.assertEqual(self.call("DELETE", "/api/invoices/%d" % draft2["id"], token="tok")[0], 200)

    def test_snooze_workflow_endpoints(self):
        _, item, _ = self.call("POST", "/api/items", {"name": "S", "expires_on": "2030-01-01"}, "tok")
        self.assertEqual(self.call("POST", "/api/items/%d/snooze" % item["id"], {"days": 5}, "tok")[1]["snoozed_until"],
                         (date.today() + timedelta(days=5)).isoformat())
        self.assertEqual(self.call("POST", "/api/items/%d/snooze" % item["id"], {"days": 99}, "tok")[0], 422)
        self.assertEqual(self.call("POST", "/api/items/%d/workflow" % item["id"], {"state": "approved"}, "tok")[1]["workflow"], "approved")
        self.assertEqual(self.call("POST", "/api/items/%d/workflow" % item["id"], {"state": "zzz"}, "tok")[0], 422)

    def test_email_link_page(self):
        _, item, _ = self.call("POST", "/api/items", {"name": "Linked", "expires_on": "2030-01-01"}, "tok")
        url = links.snooze_link(self.settings, item)
        path = url.replace("http://127.0.0.1", "")
        status, page, _ = self.call("GET", path)
        self.assertEqual(status, 200)
        self.assertIn(b"Pause reminders for Linked", page)
        conn = db.connect(self.path)
        try:
            self.assertEqual(store.get_item(conn, item["id"])["snoozed_until"], "")  # GET changed nothing
        finally:
            conn.close()
        status, page, _ = self.call("POST", path)
        self.assertIn(b"paused for 7 days", page)
        got = self.call("GET", "/api/items/%d" % item["id"], token="tok")[1]
        self.assertTrue(got["snoozed_until"])
        self.assertEqual(got["history"][0]["actor"], "email-link")
        bad = self.call("POST", path.replace("s=", "s=0"))
        self.assertEqual(bad[0], 400)
        self.assertEqual(self.call("GET", "/ack?i=x")[0], 400)

    def test_calendar_and_remind_include_links(self):
        self.call("POST", "/api/items", {"name": "Due", "expires_on": "2001-01-01", "owner_email": "o@x.co"}, "tok")
        self.call("POST", "/api/remind", {}, "tok")
        mine = [m for m in self.mail.sent if m["to"] == "o@x.co"]
        self.assertTrue(mine and "/ack?" in mine[-1]["text"])


if __name__ == "__main__":
    unittest.main()
