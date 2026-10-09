import os
import tempfile
import unittest

from thingsexpiretracker import db


class DbCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()
