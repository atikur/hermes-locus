"""Profile-scoped SQLite storage for Locus Codegraph and drift alerts.

Uses Hermes's public ``plugins.plugin_storage.plugin_db`` helper whenever running
inside Hermes so WAL mode, profile switching, and ``<HERMES_HOME>/plugin-data/locus-codegraph/``
isolation work out of the box. Falls back to standard ``sqlite3`` when invoked from
standalone unit tests or external scripts.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

PLUGIN_NAME = "locus-codegraph"
DB_FILENAME = "locus.db"
_SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    last_indexed_at REAL NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    scan_ms REAL NOT NULL DEFAULT 0,
    scan_status_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS files (
    workspace_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    lang TEXT NOT NULL,
    mtime REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0,
    pagerank REAL NOT NULL DEFAULT 0.0,
    fan_in INTEGER NOT NULL DEFAULT 0,
    fan_out INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0,
    parser_revision INTEGER NOT NULL DEFAULT 0,
    python_module_json TEXT,
    PRIMARY KEY (workspace_id, rel_path)
);

CREATE TABLE IF NOT EXISTS symbols (
    workspace_id TEXT NOT NULL,
    symbol_id TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT NOT NULL,
    min_args INTEGER NOT NULL DEFAULT 0,
    max_args INTEGER NOT NULL DEFAULT -1,
    is_exported INTEGER NOT NULL DEFAULT 1,
    docstring TEXT NOT NULL DEFAULT '',
    python_signature_json TEXT,
    PRIMARY KEY (workspace_id, symbol_id)
);

CREATE TABLE IF NOT EXISTS imports (
    workspace_id TEXT NOT NULL,
    src_path TEXT NOT NULL,
    target_spec TEXT NOT NULL,
    resolved_path TEXT NOT NULL DEFAULT '',
    imported_names TEXT NOT NULL DEFAULT '',
    line_no INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS calls (
    workspace_id TEXT NOT NULL,
    caller_path TEXT NOT NULL,
    caller_symbol TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    resolved_path TEXT NOT NULL DEFAULT '',
    line_no INTEGER NOT NULL DEFAULT 1,
    arg_count INTEGER NOT NULL DEFAULT -1,
    python_call_json TEXT,
    callee_qualname TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS drift_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    edited_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    old_signature TEXT NOT NULL,
    new_signature TEXT NOT NULL,
    reason TEXT NOT NULL,
    affected_callers_json TEXT NOT NULL DEFAULT '[]',
    resolved INTEGER NOT NULL DEFAULT 0,
    baseline_signature_json TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    target_path TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_symbols_ws_name ON symbols(workspace_id, name);
CREATE INDEX IF NOT EXISTS idx_symbols_ws_path ON symbols(workspace_id, rel_path);
CREATE INDEX IF NOT EXISTS idx_imports_ws_src ON imports(workspace_id, src_path);
CREATE INDEX IF NOT EXISTS idx_imports_ws_res ON imports(workspace_id, resolved_path);
CREATE INDEX IF NOT EXISTS idx_calls_ws_caller ON calls(workspace_id, caller_path);
CREATE INDEX IF NOT EXISTS idx_calls_ws_callee ON calls(workspace_id, callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_ws_res ON calls(workspace_id, resolved_path);
CREATE INDEX IF NOT EXISTS idx_drift_ws_open ON drift_alerts(workspace_id, resolved);
CREATE INDEX IF NOT EXISTS idx_activity_ws_time ON activity_log(workspace_id, created_at DESC);
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return

    # Serialize upgrades across concurrently starting dashboard and agent processes.
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
        for table, column, declaration in (
            ("workspaces", "scan_status_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("files", "python_module_json", "TEXT"),
            ("files", "parser_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("symbols", "python_signature_json", "TEXT"),
            ("calls", "python_call_json", "TEXT"),
            ("calls", "callee_qualname", "TEXT NOT NULL DEFAULT ''"),
            ("drift_alerts", "baseline_signature_json", "TEXT"),
        ):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    conn.commit()


def workspace_id_for(root_path: str | Path) -> str:
    """Return a deterministic 16-hex identifier for a normalized workspace root."""
    resolved = str(Path(root_path).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def _open_raw_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Open SQLite connection via Hermes plugin_storage when available, else sqlite3."""
    if db_path is not None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=15.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    env_override = os.environ.get("LOCUS_DB_PATH", "").strip()
    if env_override:
        p = Path(env_override)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=15.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    try:
        from plugins.plugin_storage import plugin_db

        return plugin_db(PLUGIN_NAME, DB_FILENAME)
    except Exception:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        fallback_dir = hermes_home / "plugin-data" / PLUGIN_NAME
        fallback_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(fallback_dir / DB_FILENAME), timeout=15.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


@contextmanager
def open_locus_db(db_path: Optional[str | Path] = None) -> Iterator[sqlite3.Connection]:
    """Context manager yielding an initialized Locus SQLite connection with Row factory."""
    conn = _open_raw_connection(db_path=db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA_SQL)
        _migrate_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
