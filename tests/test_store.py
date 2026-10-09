import unittest
from datetime import date

from thingsexpiretracker import store
from tests.helpers import DbCase

TODAY = date(2026, 6, 1)


def make(conn, **over):
    data = {"name": "Fire certificate", "expires_on": "2026-07-01", "category": "Certificate"}
    data.update(over)
    return store.create_item(conn, data, TODAY)


class StoreTests(DbCase):
    def test_create_and_present(self):
        item = make(self.conn, owner_email="a@b.co")
        self.assertEqual(item["days_left"], 30)
        self.assertEqual(item["status"], "due_soon")
        self.assertEqual(item["lead_days"], [30, 14, 7, 1])
        self.assertFalse(item["archived"])

    def test_validation_collects_all_errors(self):
        with self.assertRaises(store.ValidationError) as ctx:
            store.create_item(self.conn, {"name": " ", "expires_on": "01/07/2026", "owner_email": "nope"}, TODAY)
        self.assertEqual(set(ctx.exception.errors), {"name", "expires_on", "owner_email"})

    def test_not_found(self):
        with self.assertRaises(store.NotFound):
            store.get_item(self.conn, 99)

    def test_update_keeps_other_fields(self):
        item = make(self.conn, owner_name="Sam")
        updated = store.update_item(self.conn, item["id"], {"name": "Renamed"}, TODAY)
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual(updated["owner_name"], "Sam")

    def test_list_sorted_and_filtered(self):
        make(self.conn, name="B", expires_on="2026-09-01")
        make(self.conn, name="A", expires_on="2026-06-10", category="Licence")
        make(self.conn, name="Old", expires_on="2026-05-01")
        names = [i["name"] for i in store.list_items(self.conn, today=TODAY)]
        self.assertEqual(names, ["Old", "A", "B"])
        self.assertEqual([i["name"] for i in store.list_items(self.conn, today=TODAY, category="licence")], ["A"])
        self.assertEqual([i["name"] for i in store.list_items(self.conn, today=TODAY, status="expired")], ["Old"])
        self.assertEqual(len(store.list_items(self.conn, today=TODAY, within_days=20)), 2)
        self.assertEqual([i["name"] for i in store.list_items(self.conn, today=TODAY, query="old")], ["Old"])

    def test_archive_hides_item(self):
        item = make(self.conn)
        store.set_archived(self.conn, item["id"], True)
        self.assertEqual(store.list_items(self.conn, today=TODAY), [])
        self.assertEqual(len(store.list_items(self.conn, today=TODAY, include_archived=True)), 1)

    def test_renew_by_months_extends_from_old_expiry_when_valid(self):
        item = make(self.conn)
        renewed = store.renew_item(self.conn, item["id"], months=12, today=TODAY)
        self.assertEqual(renewed["expires_on"], "2027-07-01")
        self.assertEqual(len(store.renewals_for(self.conn, item["id"])), 1)

    def test_renew_lapsed_item_restarts_from_today(self):
        item = make(self.conn, expires_on="2026-01-01")
        renewed = store.renew_item(self.conn, item["id"], months=6, today=TODAY)
        self.assertEqual(renewed["expires_on"], "2026-12-01")

    def test_renew_must_move_forward(self):
        item = make(self.conn)
        with self.assertRaises(store.ValidationError):
            store.renew_item(self.conn, item["id"], new_expires_on="2026-06-01", today=TODAY)
        with self.assertRaises(store.ValidationError):
            store.renew_item(self.conn, item["id"], today=TODAY)

    def test_summary_counts(self):
        make(self.conn, expires_on="2026-05-01")
        make(self.conn, name="x", expires_on="2027-06-01")
        s = store.summary(self.conn, TODAY)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["counts"]["expired"], 1)
        self.assertEqual(s["counts"]["ok"], 1)


if __name__ == "__main__":
    unittest.main()
