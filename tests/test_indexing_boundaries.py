"""Incremental edit notifications must respect the scanner's workspace bounds."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from locus_core import index_workspace, reindex_edited_file, search_symbols
from locus_core.storage import open_locus_db, workspace_id_for


class TestIndexingBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "graph.db"
        (self.root / "main.py").write_text("def visible(): pass\n", encoding="utf-8")
        index_workspace(self.root, db_path=self.db)

    def test_edit_skips_gitignored_hidden_and_generated_paths(self) -> None:
        (self.root / ".gitignore").write_text("ignored/\n*.generated.py\n", encoding="utf-8")
        cases = {
            "ignored/sub/secret.py": "gitignored",
            "sample.generated.py": "gitignored",
            ".private/secret.py": "hidden_path",
            ".secret.py": "hidden_path",
            "build/generated.py": "excluded_directory",
            "nested/vendor/dependency.py": "excluded_directory",
        }
        for path, reason in cases.items():
            with self.subTest(path=path):
                target = self.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("def excluded_symbol(): pass\n", encoding="utf-8")
                result = reindex_edited_file(self.root, path, db_path=self.db)
                self.assertTrue(result["skipped"])
                self.assertEqual(result["reason"], reason)
                self.assertEqual(search_symbols(self.root, "excluded_symbol", db_path=self.db), [])
        self.assertEqual(index_workspace(self.root, force=True, db_path=self.db)["file_count"], 1)

    def test_edit_does_not_follow_symlinked_directory_inside_workspace(self) -> None:
        actual = self.root / "actual"
        actual.mkdir()
        (actual / "module.py").write_text("def excluded_symbol(): pass\n", encoding="utf-8")
        os.symlink(actual, self.root / "link")
        result = reindex_edited_file(self.root, "link/module.py", db_path=self.db)
        self.assertEqual(result["reason"], "symlink")
        self.assertEqual(search_symbols(self.root, "excluded_symbol", db_path=self.db), [])

    def test_deletion_removes_previously_indexed_path_after_ignore_change(self) -> None:
        (self.root / ".gitignore").write_text("main.py\n", encoding="utf-8")
        (self.root / "main.py").unlink()
        result = reindex_edited_file(self.root, "main.py", db_path=self.db)
        self.assertTrue(result["ok"])
        self.assertEqual(search_symbols(self.root, "visible", db_path=self.db), [])
        with open_locus_db(db_path=self.db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM files WHERE workspace_id = ?",
                                 (workspace_id_for(self.root),)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_scan_reports_truncation_only_when_more_files_exist(self) -> None:
        (self.root / "other.py").write_text("def other(): pass\n", encoding="utf-8")
        complete = index_workspace(self.root, max_files=2, db_path=self.db)
        self.assertFalse(complete["truncated"])
        (self.root / "third.py").write_text("def third(): pass\n", encoding="utf-8")
        limited = index_workspace(self.root, max_files=2, db_path=self.db)
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["max_files"], 2)
        self.assertEqual(limited["file_count"], 2)


if __name__ == "__main__":
    unittest.main()
