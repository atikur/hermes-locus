"""Locus Codegraph must not share another plugin's Locus data namespace."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from locus_core import open_locus_db


class TestPluginIdentity(unittest.TestCase):
    def test_default_database_is_isolated_from_existing_locus_plugin_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            existing = home / "plugin-data" / "locus" / "locus.db"
            existing.parent.mkdir(parents=True)
            with sqlite3.connect(existing) as conn:
                conn.execute("CREATE TABLE unrelated_plugin_state (value TEXT)")
                conn.execute("INSERT INTO unrelated_plugin_state VALUES ('preserve me')")

            with patch.dict(os.environ, {"HERMES_HOME": str(home), "LOCUS_DB_PATH": ""}):
                with open_locus_db() as conn:
                    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
                    self.assertIn("workspaces", tables)
                    self.assertNotIn("unrelated_plugin_state", tables)
                    database = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
                    self.assertEqual(database.resolve(), (home / "plugin-data/locus-codegraph/locus.db").resolve())

            with sqlite3.connect(existing) as conn:
                self.assertEqual(conn.execute("SELECT value FROM unrelated_plugin_state").fetchone()[0], "preserve me")
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'workspaces'").fetchone())


if __name__ == "__main__":
    unittest.main()
