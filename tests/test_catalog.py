"""Startup migrations, against databases built by older schemas."""

import os
import shutil
import tempfile
import unittest

from discstakka.catalog import db


class Migrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="discstakka-db-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "catalog.db")

    def old_database(self, kind_columns):
        conn = db.connect(self.path)
        with conn:
            conn.execute(
                "CREATE TABLE disc (id INTEGER PRIMARY KEY,"
                " slot INTEGER NOT NULL UNIQUE, title TEXT NOT NULL, subtitle TEXT,"
                " %s, notes TEXT, art_path TEXT,"
                " status TEXT NOT NULL DEFAULT 'stored', checked_out_at TEXT,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)" % kind_columns
            )
            conn.execute(
                "INSERT INTO disc (slot, title, status, created_at, updated_at)"
                " VALUES (4, 'Ico', 'stored', 'then', 'then')"
            )
        conn.close()

    def check(self):
        db.init(self.path)
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        row = db.get_by_slot(conn, 4)
        self.assertEqual(row["title"], "Ico")
        self.assertIsNone(row["parent_id"])
        self.assertIsNone(row["disc_no"])
        db.create_disc(conn, 5, None, parent_id=row["id"])
        self.assertEqual(len(db.set_discs(conn, row["id"])), 2)

    def test_a_catalogue_from_before_sets_gains_them(self):
        self.old_database("category TEXT, platform TEXT")
        self.check()

    def test_a_catalogue_from_before_categories_gains_both(self):
        self.old_database("media_type TEXT")
        self.check()


if __name__ == "__main__":
    unittest.main()
