"""Signature drift detection across workspace callers when a file is edited.

Compares pre-edit vs. post-edit symbols of an edited module and inspects the
workspace's call/import tables to catch broken external callers immediately.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .parsers import ParsedSymbol


@dataclass(slots=True)
class DriftAlert:
    edited_path: str
    symbol_name: str
    severity: str  # "critical" | "warning"
    old_signature: str
    new_signature: str
    reason: str
    affected_callers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edited_path": self.edited_path,
            "symbol_name": self.symbol_name,
            "severity": self.severity,
            "old_signature": self.old_signature,
            "new_signature": self.new_signature,
            "reason": self.reason,
            "affected_callers": self.affected_callers,
        }


def _external_callers_for_symbol(
    conn: sqlite3.Connection,
    workspace_id: str,
    edited_path: str,
    symbol_name: str,
) -> list[dict[str, Any]]:
    """Find external call sites and explicit importers referencing *symbol_name*."""
    rows = conn.execute(
        """
        SELECT caller_path, caller_symbol, line_no, arg_count
        FROM calls
        WHERE workspace_id = ?
          AND callee_name = ?
          AND caller_path != ?
          AND (resolved_path = ? OR resolved_path = '')
        ORDER BY caller_path, line_no
        LIMIT 25
        """,
        (workspace_id, symbol_name, edited_path, edited_path),
    ).fetchall()

    callers: list[dict[str, Any]] = [
        {
            "path": r["caller_path"],
            "caller": r["caller_symbol"],
            "line": int(r["line_no"]),
            "arg_count": int(r["arg_count"]),
            "kind": "call",
        }
        for r in rows
    ]

    if not callers:
        imp_rows = conn.execute(
            """
            SELECT src_path, imported_names, line_no
            FROM imports
            WHERE workspace_id = ?
              AND resolved_path = ?
              AND src_path != ?
            ORDER BY src_path, line_no
            LIMIT 25
            """,
            (workspace_id, edited_path, edited_path),
        ).fetchall()
        for r in imp_rows:
            names = [n.strip() for n in (r["imported_names"] or "").split(",") if n.strip()]
            if symbol_name in names:
                callers.append(
                    {
                        "path": r["src_path"],
                        "caller": "<import>",
                        "line": int(r["line_no"]),
                        "arg_count": -1,
                        "kind": "import",
                    }
                )
    return callers


def detect_and_record_drift(
    conn: sqlite3.Connection,
    workspace_id: str,
    edited_path: str,
    old_symbols: dict[str, ParsedSymbol],
    new_symbols: dict[str, ParsedSymbol],
) -> list[DriftAlert]:
    """Compare old vs. new symbol definitions for *edited_path* and persist open drift alerts."""
    alerts: list[DriftAlert] = []
    now = time.time()

    # Mark previous open alerts on this edited_path as resolved before re-evaluating
    conn.execute(
        "UPDATE drift_alerts SET resolved = 1 WHERE workspace_id = ? AND edited_path = ? AND resolved = 0",
        (workspace_id, edited_path),
    )

    # 1. Deleted exported symbols that external files still call or import
    for qname, old_sym in old_symbols.items():
        if not old_sym.is_exported or qname in new_symbols:
            continue
        callers = _external_callers_for_symbol(conn, workspace_id, edited_path, old_sym.name)
        if callers:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=old_sym.qualified_name,
                    severity="critical",
                    old_signature=old_sym.signature,
                    new_signature="<removed>",
                    reason=(
                        f"Exported symbol '{old_sym.qualified_name}' was removed, "
                        f"but {len(callers)} external site(s) still reference it."
                    ),
                    affected_callers=callers,
                )
            )

    # 2. Modified signatures where arity or parameter contract breaks external callers
    for qname, new_sym in new_symbols.items():
        old_sym = old_symbols.get(qname)
        if old_sym is None:
            continue
        if old_sym.signature == new_sym.signature and old_sym.min_args == new_sym.min_args and old_sym.max_args == new_sym.max_args:
            continue

        callers = _external_callers_for_symbol(conn, workspace_id, edited_path, new_sym.name)
        if not callers:
            continue

        broken_arity_callers = [
            c
            for c in callers
            if c["arg_count"] >= 0
            and (
                c["arg_count"] < new_sym.min_args
                or (new_sym.max_args >= 0 and c["arg_count"] > new_sym.max_args)
            )
        ]

        if broken_arity_callers:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=new_sym.qualified_name,
                    severity="critical",
                    old_signature=old_sym.signature,
                    new_signature=new_sym.signature,
                    reason=(
                        f"Signature of '{new_sym.qualified_name}' now accepts "
                        f"{new_sym.min_args}..{'*' if new_sym.max_args < 0 else new_sym.max_args} arg(s) "
                        f"(was {old_sym.min_args}..{'*' if old_sym.max_args < 0 else old_sym.max_args}); "
                        f"{len(broken_arity_callers)} external call site(s) pass incompatible argument counts."
                    ),
                    affected_callers=broken_arity_callers,
                )
            )
        elif new_sym.min_args > old_sym.min_args:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=new_sym.qualified_name,
                    severity="warning",
                    old_signature=old_sym.signature,
                    new_signature=new_sym.signature,
                    reason=(
                        f"Required parameter count of '{new_sym.qualified_name}' increased from "
                        f"{old_sym.min_args} to {new_sym.min_args}; verify {len(callers)} external caller(s)."
                    ),
                    affected_callers=callers,
                )
            )

    for a in alerts:
        conn.execute(
            """
            INSERT INTO drift_alerts (
                workspace_id, created_at, edited_path, symbol_name, severity,
                old_signature, new_signature, reason, affected_callers_json, resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                workspace_id,
                now,
                a.edited_path,
                a.symbol_name,
                a.severity,
                a.old_signature,
                a.new_signature,
                a.reason,
                json.dumps(a.affected_callers),
            ),
        )
    return alerts


def list_open_drift_alerts(
    conn: sqlite3.Connection,
    workspace_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, created_at, edited_path, symbol_name, severity,
               old_signature, new_signature, reason, affected_callers_json
        FROM drift_alerts
        WHERE workspace_id = ? AND resolved = 0
        ORDER BY id DESC
        LIMIT ?
        """,
        (workspace_id, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            callers = json.loads(r["affected_callers_json"] or "[]")
        except Exception:
            callers = []
        out.append(
            {
                "id": int(r["id"]),
                "created_at": float(r["created_at"]),
                "edited_path": r["edited_path"],
                "symbol_name": r["symbol_name"],
                "severity": r["severity"],
                "old_signature": r["old_signature"],
                "new_signature": r["new_signature"],
                "reason": r["reason"],
                "affected_callers": callers,
            }
        )
    return out
