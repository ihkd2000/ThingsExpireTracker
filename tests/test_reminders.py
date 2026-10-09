import unittest
from datetime import date

from thingsexpiretracker import reminders, store
from thingsexpiretracker.notify import RecordingNotifier
from tests.helpers import DbCase


def add(conn, name, expires, email="owner@x.co", **extra):
    data = {"name": name, "expires_on": expires, "owner_email": email}
    data.update(extra)
    return store.create_item(conn, data)


class ReminderTests(DbCase):
    def test_groups_per_recipient(self):
        add(self.conn, "A", "2026-06-10")
        add(self.conn, "B", "2026-06-12")
        add(self.conn, "C", "2026-06-12", email="other@x.co")
        n = RecordingNotifier()
        r = reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertEqual((r.sent, r.items_notified), (2, 3))
        self.assertEqual(sorted(m["to"] for m in n.sent), ["other@x.co", "owner@x.co"])

    def test_same_stage_not_sent_twice(self):
        add(self.conn, "A", "2026-06-10")
        n = RecordingNotifier()
        reminders.run(self.conn, n, date(2026, 6, 9))
        reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertEqual(len(n.sent), 1)

    def test_next_stage_sends_again(self):
        add(self.conn, "A", "2026-06-10")
        n = RecordingNotifier()
        reminders.run(self.conn, n, date(2026, 6, 3))   # 7 days left
        reminders.run(self.conn, n, date(2026, 6, 9))   # 1 day left
        reminders.run(self.conn, n, date(2026, 6, 10))  # today
        self.assertEqual(len(n.sent), 3)

    def test_failed_delivery_is_retried(self):
        add(self.conn, "A", "2026-06-10")
        n = RecordingNotifier(fail=True)
        r = reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertEqual(r.failed, 1)
        n.fail = False
        r = reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertEqual(r.sent, 1)
        self.assertEqual(len(n.sent), 1)

    def test_overdue_reminds_weekly(self):
        add(self.conn, "A", "2026-06-01")
        n = RecordingNotifier()
        for day in (2, 3, 8, 9, 10):
            reminders.run(self.conn, n, date(2026, 6, day))
        self.assertEqual(len(n.sent), 2)  # first week, second week

    def test_no_recipient_is_reported_not_lost(self):
        add(self.conn, "Orphan", "2026-06-10", email="")
        n = RecordingNotifier()
        r = reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertEqual(r.no_recipient, ["Orphan"])
        self.assertEqual(n.sent, [])
        r = reminders.run(self.conn, n, date(2026, 6, 9), default_recipient="boss@x.co")
        self.assertEqual(r.sent, 1)

    def test_dry_run_sends_and_logs_nothing(self):
        add(self.conn, "A", "2026-06-10")
        n = RecordingNotifier()
        r = reminders.run(self.conn, n, date(2026, 6, 9), dry_run=True)
        self.assertEqual(len(r.planned), 1)
        self.assertEqual(n.sent, [])
        self.assertEqual(reminders.run(self.conn, n, date(2026, 6, 9)).sent, 1)

    def test_renewal_starts_a_fresh_cycle(self):
        item = add(self.conn, "A", "2026-06-10")
        n = RecordingNotifier()
        reminders.run(self.conn, n, date(2026, 6, 9))
        store.renew_item(self.conn, item["id"], new_expires_on="2026-06-30", today=date(2026, 6, 9))
        reminders.run(self.conn, n, date(2026, 6, 29))
        self.assertEqual(len(n.sent), 2)

    def test_message_escapes_html(self):
        add(self.conn, "<script>x</script>", "2026-06-10")
        n = RecordingNotifier()
        reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertNotIn("<script>", n.sent[0]["html"])
        self.assertIn("&lt;script&gt;", n.sent[0]["html"])

    def test_archived_items_are_not_reminded(self):
        item = add(self.conn, "A", "2026-06-10")
        store.set_archived(self.conn, item["id"], True)
        n = RecordingNotifier()
        reminders.run(self.conn, n, date(2026, 6, 9))
        self.assertEqual(n.sent, [])


if __name__ == "__main__":
    unittest.main()
