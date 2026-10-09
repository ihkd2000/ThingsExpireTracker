import unittest
from datetime import date

from thingsexpiretracker import rules


class RulesTests(unittest.TestCase):
    def test_parse_leads(self):
        self.assertEqual(rules.parse_leads("7, 30,14,7"), (30, 14, 7))
        self.assertEqual(tuple(rules.parse_leads("")), tuple(rules.DEFAULT_LEADS))
        with self.assertRaises(ValueError):
            rules.parse_leads("a,b")
        with self.assertRaises(ValueError):
            rules.parse_leads("-3")

    def test_status(self):
        today = date(2026, 6, 1)
        self.assertEqual(rules.status(date(2026, 7, 15), today), "ok")
        self.assertEqual(rules.status(date(2026, 7, 1), today), "due_soon")
        self.assertEqual(rules.status(today, today), "due_today")
        self.assertEqual(rules.status(date(2026, 5, 31), today), "expired")

    def test_stage_picks_most_urgent_reached_lead(self):
        leads = [30, 14, 7, 1]
        self.assertIsNone(rules.reminder_stage(31, leads))
        self.assertEqual(rules.reminder_stage(30, leads), 30)
        self.assertEqual(rules.reminder_stage(10, leads), 14)
        self.assertEqual(rules.reminder_stage(1, leads), 1)
        self.assertEqual(rules.reminder_stage(0, leads), 0)

    def test_overdue_stages_are_weekly(self):
        leads = [30]
        self.assertEqual(rules.reminder_stage(-1, leads), -1)
        self.assertEqual(rules.reminder_stage(-7, leads), -1)
        self.assertEqual(rules.reminder_stage(-8, leads), -2)
        self.assertEqual(rules.reminder_stage(-15, leads), -3)

    def test_expiry_date_is_still_due_today_not_expired(self):
        today = date(2026, 10, 9)
        self.assertEqual(rules.status(today, today), rules.STATUS_DUE_TODAY)
        self.assertEqual(rules.status(date(2026, 10, 8), today), rules.STATUS_EXPIRED)

    def test_custom_lead_window_boundaries(self):
        today = date(2026, 10, 9)
        self.assertEqual(rules.status(date(2026, 10, 19), today, [7, 1]), rules.STATUS_OK)
        self.assertEqual(rules.status(date(2026, 10, 16), today, [7, 1]), rules.STATUS_DUE_SOON)

    def test_empty_lead_list_uses_default_status_window(self):
        today = date(2026, 10, 9)
        self.assertEqual(rules.status(date(2026, 11, 8), today, []), rules.STATUS_DUE_SOON)

    def test_reminder_stages_cross_week_boundary(self):
        self.assertEqual(rules.reminder_stage(-14), -2)
        self.assertEqual(rules.reminder_stage(-15), -3)

    def test_add_months_is_calendar_safe(self):
        self.assertEqual(rules.add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(rules.add_months(date(2023, 1, 31), 1), date(2023, 2, 28))
        self.assertEqual(rules.add_months(date(2024, 12, 15), 2), date(2025, 2, 15))


if __name__ == "__main__":
    unittest.main()
