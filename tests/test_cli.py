import io
import json
import os
import tempfile
import unittest

from thingsexpiretracker.cli import main
from thingsexpiretracker.config import Settings
from thingsexpiretracker.notify import RecordingNotifier


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "c.db")

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, *argv, notifier=None, settings=None, serve=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--db", self.db] + list(argv), out=out, err=err,
                    settings=settings or Settings(), notifier=notifier, serve=serve)
        return code, out.getvalue(), err.getvalue()

    def test_add_list_show_renew_archive(self):
        code, out, _ = self.run_cli("add", "Insurance", "2030-01-01", "--email", "a@b.co")
        self.assertEqual(code, 0)
        self.assertIn("Added #1", out)
        self.assertIn("Insurance", self.run_cli("list")[1])
        self.assertIn("a@b.co", self.run_cli("show", "1")[1])
        code, out, _ = self.run_cli("renew", "1", "--months", "12")
        self.assertIn("2031-01-01", out)
        self.assertIn("Renewals", self.run_cli("show", "1")[1])
        self.run_cli("archive", "1")
        self.assertIn("No items", self.run_cli("list")[1])
        self.assertIn("Insurance", self.run_cli("list", "--all")[1])

    def test_validation_error_exit_code(self):
        code, _, err = self.run_cli("add", "x", "2030-01-01", "--email", "bad")
        self.assertEqual(code, 1)
        self.assertIn("owner_email", err)

    def test_missing_item(self):
        code, _, err = self.run_cli("show", "42")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_remind_and_dry_run(self):
        self.run_cli("add", "Cert", "2026-06-10", "--email", "a@b.co")
        n = RecordingNotifier()
        _, out, _ = self.run_cli("remind", "--dry-run", "--today", "2026-06-09", notifier=n)
        self.assertIn("Would email a@b.co", out)
        self.assertEqual(n.sent, [])
        _, out, _ = self.run_cli("remind", "--today", "2026-06-09", notifier=n)
        self.assertIn("Sent 1 email", out)
        self.assertEqual(len(n.sent), 1)

    def test_remind_failure_exit_code(self):
        self.run_cli("add", "Cert", "2026-06-10", "--email", "a@b.co")
        code, out, _ = self.run_cli("remind", "--today", "2026-06-09", notifier=RecordingNotifier(fail=True))
        self.assertEqual(code, 1)
        self.assertIn("1 failed", out)

    def test_import_export(self):
        path = os.path.join(self._dir.name, "in.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("name,expires_on\nA,2030-01-01\nB,bad\n")
        code, out, err = self.run_cli("import", path)
        self.assertEqual(code, 1)
        self.assertIn("Imported 1", out)
        self.assertIn("line 3", err)
        _, out, _ = self.run_cli("export")
        self.assertIn("A,", out)
        self.assertEqual(self.run_cli("import", os.path.join(self._dir.name, "nope.csv"))[0], 2)

    def test_cost_summary_history_json(self):
        self.run_cli("add", "Insurance", "2099-01-01", "--cost", "1200.5", "--vendor", "Acme")
        out = self.run_cli("list", "--json")[1]
        data = json.loads(out)
        self.assertEqual((data[0]["cost"], data[0]["vendor"]), ("1200.50", "Acme"))
        self.assertEqual(json.loads(self.run_cli("show", "1", "--json")[1])["history"][0]["action"], "created")
        self.assertIn("Tracking 1 item", self.run_cli("summary")[1])
        self.assertEqual(json.loads(self.run_cli("summary", "--json")[1])["total"], 1)
        self.run_cli("renew", "1", "--months", "12")
        text = self.run_cli("history", "1")[1]
        self.assertIn("renewed", text)
        self.assertIn("cli", text)
        self.assertIn("Insurance", self.run_cli("history")[1])
        self.assertEqual(self.run_cli("add", "x", "2099-01-01", "--cost", "lots")[0], 1)

    def test_ics_and_backup(self):
        self.run_cli("add", "Cert", "2099-01-01")
        self.assertIn("BEGIN:VEVENT", self.run_cli("ics")[1])
        target = os.path.join(self._dir.name, "backup.db")
        self.assertEqual(self.run_cli("backup", target)[0], 0)
        import sqlite3
        self.assertEqual(sqlite3.connect(target).execute("SELECT count(*) FROM items").fetchone()[0], 1)

    def test_escalation_shows_in_dry_run(self):
        self.run_cli("add", "Lapsed", "2026-06-01", "--email", "o@x.co")
        _, out, _ = self.run_cli("remind", "--dry-run", "--today", "2026-06-10", settings=Settings(escalate_to="boss@x.co"))
        self.assertIn("Would email boss@x.co", out)
        self.assertIn("[escalation]", out)

    def test_snooze_workflow_attach_user(self):
        self.run_cli("add", "Policy", "2099-01-01", "--email", "a@b.co")
        self.assertIn("paused until", self.run_cli("snooze", "1", "7")[1])
        self.assertEqual(self.run_cli("snooze", "1", "99")[0], 1)
        self.assertIn("approved", self.run_cli("workflow", "1", "approved")[1])
        self.assertIn("not started", self.run_cli("workflow", "1", "none")[1])
        path = os.path.join(self._dir.name, "note.txt")
        with open(path, "w") as fh:
            fh.write("hello")
        self.assertIn("as file 1", self.run_cli("attach", "1", path)[1])
        self.assertIn("note.txt", self.run_cli("attachments", "1")[1])
        out_path = os.path.join(self._dir.name, "back.txt")
        self.run_cli("attachments", "1", "--save", "1", "--to", out_path)
        self.assertEqual(_read(out_path), "hello")
        bad = os.path.join(self._dir.name, "x.exe")
        _write(bad, b"MZ")
        self.assertEqual(self.run_cli("attach", "1", bad)[0], 1)
        self.assertIn("Created admin", self.run_cli("user", "add", "root", "--role", "admin", "--password", "long enough password")[1])
        self.assertEqual(self.run_cli("user", "add", "root", "--password", "long enough password")[0], 1)
        self.assertIn("root", self.run_cli("user", "list")[1])
        self.assertEqual(self.run_cli("user", "disable", "root")[0], 1)  # last admin
        self.assertEqual(self.run_cli("user", "role", "ghost", "viewer")[0], 1)

    def test_invoice_commands(self):
        from tests.pdfmaker import make_pdf
        from tests.test_v3 import INVOICE_LINES
        self.run_cli("add", "Hosting", "2099-01-01", "--vendor", "Acme Hosting Ltd")
        path = os.path.join(self._dir.name, "inv.pdf")
        _write(path, make_pdf(INVOICE_LINES))
        code, out, _ = self.run_cli("invoice", "add", path)
        self.assertEqual(code, 0)
        self.assertIn("1249.50", out)
        self.assertIn("draft", self.run_cli("invoice", "list")[1])
        code, out, _ = self.run_cli("invoice", "confirm", "1", "--amount", "1200", "--update-cost")
        self.assertIn("Recorded invoice #1: 1200.00", out)
        self.assertIn("1200.00", self.run_cli("show", "1")[1])
        stray = os.path.join(self._dir.name, "s.txt")
        _write(stray, b"Nobody\nTotal: $3.00\n")
        code, _, err = self.run_cli("invoice", "add", stray)
        self.assertEqual(code, 1)
        self.assertIn("--item", err)
        self.assertEqual(self.run_cli("invoice", "add", stray, "--item", "1")[0], 0)
        self.assertIn("Discarded", self.run_cli("invoice", "discard", "2")[1])

    def test_serve_passes_arguments_and_reports_bad_config(self):
        seen = {}

        def fake(settings, db_path, host, port, out=None):
            seen.update(host=host, port=port, db=db_path)

        self.assertEqual(self.run_cli("serve", "--port", "9000", serve=fake)[0], 0)
        self.assertEqual((seen["host"], seen["port"]), ("127.0.0.1", 9000))

        def refuse(*a, **k):
            raise ValueError("Set THINGSEXPIRE_TOKEN first")

        code, _, err = self.run_cli("serve", serve=refuse)
        self.assertEqual(code, 2)
        self.assertIn("THINGSEXPIRE_TOKEN", err)


if __name__ == "__main__":
    unittest.main()
