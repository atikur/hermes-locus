"""An existing index upgrades without dropping its alert history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from locus_core import index_workspace
from locus_core.storage import open_locus_db, workspace_id_for


_LEGACY_TABLES = """
CREATE TABLE files (
    workspace_id TEXT NOT NULL, rel_path TEXT NOT NULL, lang TEXT NOT NULL,
    mtime REAL NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0, pagerank REAL NOT NULL DEFAULT 0.0,
    fan_in INTEGER NOT NULL DEFAULT 0, fan_out INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (workspace_id, rel_path)
);
CREATE TABLE symbols (
    workspace_id TEXT NOT NULL, symbol_id TEXT NOT NULL, name TEXT NOT NULL,
    qualified_name TEXT NOT NULL, kind TEXT NOT NULL, rel_path TEXT NOT NULL,
    line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, signature TEXT NOT NULL,
    min_args INTEGER NOT NULL DEFAULT 0, max_args INTEGER NOT NULL DEFAULT -1,
    is_exported INTEGER NOT NULL DEFAULT 1, docstring TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, symbol_id)
);
CREATE TABLE calls (
    workspace_id TEXT NOT NULL, caller_path TEXT NOT NULL, caller_symbol TEXT NOT NULL,
    callee_name TEXT NOT NULL, resolved_path TEXT NOT NULL DEFAULT '',
    line_no INTEGER NOT NULL DEFAULT 1, arg_count INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE drift_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL,
    created_at REAL NOT NULL, edited_path TEXT NOT NULL, symbol_name TEXT NOT NULL,
    severity TEXT NOT NULL, old_signature TEXT NOT NULL, new_signature TEXT NOT NULL,
    reason TEXT NOT NULL, affected_callers_json TEXT NOT NULL DEFAULT '[]',
    resolved INTEGER NOT NULL DEFAULT 0
);
"""


class TestStorageMigration(unittest.TestCase):
    def test_refreshes_unchanged_python_and_preserves_open_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            db = Path(tmp) / "locus.db"
            ws_id = workspace_id_for(root)
            sources = {
                "library.py": "def verify(token, *, audience):\n    return True\n",
                "caller.py": "from library import verify\nverify(token='test')\n",
            }
            with sqlite3.connect(db) as conn:
                conn.executescript(_LEGACY_TABLES)
                for rel_path, source in sources.items():
                    path = root / rel_path
                    raw = source.encode()
                    path.write_bytes(raw)
                    conn.execute(
                        """
                        INSERT INTO files (workspace_id, rel_path, lang, mtime, size_bytes, sha256)
                        VALUES (?, ?, 'python', ?, ?, ?)
                        """,
                        (ws_id, rel_path, path.stat().st_mtime, len(raw), hashlib.sha256(raw).hexdigest()),
                    )
                conn.execute(
                    """
                    INSERT INTO drift_alerts (
                        id, workspace_id, created_at, edited_path, symbol_name, severity,
                        old_signature, new_signature, reason
                    ) VALUES (7, ?, 123, 'library.py', 'verify', 'critical',
                              'def verify(token)', 'def verify(token, *, audience)', 'missing argument')
                    """,
                    (ws_id,),
                )

            result = index_workspace(root, db_path=db)
            self.assertEqual(result["updated_files"], 2)
            self.assertEqual(index_workspace(root, db_path=db)["updated_files"], 0)
            with open_locus_db(db_path=db) as conn:
                signature = conn.execute("SELECT python_signature_json FROM symbols WHERE name = 'verify'").fetchone()[0]
                call = conn.execute("SELECT python_call_json FROM calls WHERE callee_name = 'verify'").fetchone()[0]
                alert = conn.execute("SELECT * FROM drift_alerts").fetchone()
            self.assertEqual(json.loads(signature)["keyword_only"], ["audience"])
            self.assertEqual(json.loads(call)["keywords"], ["token"])
            self.assertEqual(alert["id"], 7)
            self.assertEqual(alert["created_at"], 123)
            self.assertEqual(alert["old_signature"], "def verify(token)")
            self.assertEqual(alert["resolved"], 0)
            self.assertIsNone(alert["baseline_signature_json"])


if __name__ == "__main__":
    unittest.main()
