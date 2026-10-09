import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from thingsexpiretracker.config import Settings
from thingsexpiretracker.notify import RecordingNotifier
from thingsexpiretracker.web import make_server


class WebTests(unittest.TestCase):
    TOKEN = "s3cret"

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.notifier = RecordingNotifier()
        cls.server = make_server(Settings(api_token=cls.TOKEN, default_recipient="boss@x.co"),
                                 os.path.join(cls._dir.name, "w.db"), "127.0.0.1", 0, notifier=cls.notifier)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._dir.cleanup()

    def call(self, method, path, body=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def test_health_and_dashboard_need_no_token(self):
        self.assertEqual(self.call("GET", "/api/health", token=None)[0], 200)
        with urllib.request.urlopen(self.base + "/") as resp:
            html = resp.read().decode()
        self.assertIn("ThingsExpireTracker", html)

    def test_api_requires_token(self):
        self.assertEqual(self.call("GET", "/api/items", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/items", token="wrong")[0], 401)

    def test_item_lifecycle(self):
        status, item = self.call("POST", "/api/items", {"name": "Domain", "expires_on": "2030-01-01", "owner_email": "a@b.co"})
        self.assertEqual(status, 201)
        iid = item["id"]
        self.assertEqual(self.call("GET", "/api/items/%d" % iid)[1]["name"], "Domain")
        status, upd = self.call("PUT", "/api/items/%d" % iid, {"notes": "auto-renews"})
        self.assertEqual((status, upd["notes"]), (200, "auto-renews"))
        status, ren = self.call("POST", "/api/items/%d/renew" % iid, {"months": 12})
        self.assertEqual((status, ren["expires_on"]), (200, "2031-01-01"))
        self.assertEqual(len(self.call("GET", "/api/items/%d" % iid)[1]["renewals"]), 1)
        self.assertEqual(self.call("DELETE", "/api/items/%d" % iid)[0], 200)
        items = self.call("GET", "/api/items")[1]["items"]
        self.assertNotIn(iid, [i["id"] for i in items])
        self.assertEqual(self.call("POST", "/api/items/%d/unarchive" % iid)[0], 200)

    def test_validation_and_errors(self):
        status, body = self.call("POST", "/api/items", {"name": "", "expires_on": "x"})
        self.assertEqual(status, 422)
        self.assertIn("name", body["fields"])
        self.assertEqual(self.call("GET", "/api/items/9999")[0], 404)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/api/items", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/items", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", "/api/items/1/renew", {"months": "12"})[0], 422)

    def test_body_limit(self):
        self.assertEqual(self.call("POST", "/api/items", raw=b" " * (70 * 1024))[0], 413)

    def test_remind_endpoint(self):
        self.call("POST", "/api/items", {"name": "Soon", "expires_on": "2000-01-01"})
        status, body = self.call("POST", "/api/remind", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["sent"], 1)
        self.assertIn("boss@x.co", [m["to"] for m in self.notifier.sent])

    def test_summary(self):
        self.assertIn("counts", self.call("GET", "/api/summary")[1])

    def raw(self, method, path, data=None, token=TOKEN):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read().decode("utf-8")

    def test_import_export_roundtrip(self):
        csv_text = "name,expires_on,cost,vendor\nImported thing,2031-02-03,42.5,Acme\nBad row,nope,,\n"
        status, body = self.call("POST", "/api/import", raw=csv_text.encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["imported"], 1)
        self.assertIn("line 3", body["errors"][0])
        status, ctype, text = self.raw("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        self.assertIn("Imported thing", text)
        self.assertIn("42.50", text)
        self.assertEqual(self.call("POST", "/api/import?strict=1", raw=b"name,expires_on\nA,2030-01-01\nB,x\n")[1]["imported"], 0)

    def test_calendar_accepts_query_token_only_for_the_feed(self):
        self.call("POST", "/api/items", {"name": "Cal item", "expires_on": "2032-01-01"})
        status, ctype, text = self.raw("GET", "/api/calendar.ics?token=" + self.TOKEN, token=None)
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", ctype)
        self.assertIn("Expires: Cal item", text)
        self.assertEqual(self.raw("GET", "/api/calendar.ics?token=wrong", token=None)[0], 401)
        self.assertEqual(self.raw("GET", "/api/items?token=" + self.TOKEN, token=None)[0], 401)

    def test_detail_has_history_and_activity_feed(self):
        _, item = self.call("POST", "/api/items", {"name": "Audited", "expires_on": "2033-01-01", "cost": "10"})
        self.call("PUT", "/api/items/%d" % item["id"], {"vendor": "V"})
        detail = self.call("GET", "/api/items/%d" % item["id"])[1]
        self.assertEqual([h["action"] for h in detail["history"]], ["updated", "created"])
        self.assertEqual(detail["history"][0]["actor"], "token")
        self.assertEqual(detail["reminders"], [])
        self.assertTrue(self.call("GET", "/api/activity?limit=5")[1]["activity"])
        self.assertEqual(self.call("POST", "/api/items", {"name": "bad", "expires_on": "2033-01-01", "cost": "lots"})[0], 422)

    def test_remind_dry_run_lists_plan(self):
        self.call("POST", "/api/items", {"name": "Plan me", "expires_on": "2001-01-01", "owner_email": "z@x.co"})
        status, body = self.call("POST", "/api/remind", {"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(any(p["item"] == "Plan me" and p["to"] == "z@x.co" for p in body["planned"]))

    def test_dashboard_is_served_with_security_headers(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertIn("default-src", resp.headers["Content-Security-Policy"])
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
            html = resp.read().decode()
        self.assertNotIn("__APP__", html)
        self.assertNotIn("__CURRENCY__", html)

    def test_health_reports_version(self):
        self.assertIn("version", self.call("GET", "/api/health", token=None)[1])


class BindSafetyTests(unittest.TestCase):
    def test_remote_bind_requires_token(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                make_server(Settings(), os.path.join(d, "x.db"), "0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
