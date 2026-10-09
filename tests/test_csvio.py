import io
import unittest

from thingsexpiretracker import csvio, store
from tests.helpers import DbCase

GOOD = "name,category,owner_email,expires_on\nPassport,ID,a@b.co,2030-01-01\nLicence,ID,,2029-05-05\n"


class CsvTests(DbCase):
    def test_import_and_duplicates(self):
        r = csvio.import_csv(self.conn, io.StringIO(GOOD))
        self.assertEqual((r.imported, r.errors), (2, []))
        again = csvio.import_csv(self.conn, io.StringIO(GOOD))
        self.assertEqual((again.imported, again.skipped_duplicates), (0, 2))

    def test_errors_have_line_numbers(self):
        text = "name,expires_on\nOk,2030-01-01\n,2030-01-01\nBad,tomorrow\n"
        r = csvio.import_csv(self.conn, io.StringIO(text))
        self.assertEqual(r.imported, 1)
        self.assertEqual(len(r.errors), 2)
        self.assertTrue(r.errors[0].startswith("line 3:"))
        self.assertTrue(r.errors[1].startswith("line 4:"))

    def test_strict_imports_nothing(self):
        text = "name,expires_on\nOk,2030-01-01\nBad,nope\n"
        r = csvio.import_csv(self.conn, io.StringIO(text), strict=True)
        self.assertEqual(r.imported, 0)
        self.assertEqual(store.list_items(self.conn), [])

    def test_missing_columns(self):
        r = csvio.import_csv(self.conn, io.StringIO("name\nx\n"))
        self.assertIn("Missing column", r.errors[0])

    def test_export_guards_formulas_and_round_trips(self):
        store.create_item(self.conn, {"name": "=HYPERLINK(\"x\")", "expires_on": "2030-01-01"})
        text = csvio.export_csv(store.list_items(self.conn))
        self.assertIn("'=HYPERLINK", text)
        self.assertEqual(text.splitlines()[0], ",".join(csvio.COLUMNS))


if __name__ == "__main__":
    unittest.main()
