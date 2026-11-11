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
    *,
    include_imports: bool = True,
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

    if include_imports:
        imp_rows = conn.execute(
            """
            SELECT src_path, imported_names, line_no
            FROM imports
            WHERE workspace_id = ?
              AND (resolved_path = ? OR resolved_path = '')
              AND src_path != ?
            ORDER BY src_path, line_no
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


def _accepts_arg_count(min_args: int, max_args: int, count: int) -> bool:
    return count >= min_args and (max_args < 0 or count <= max_args)


def _record_alert(
    conn: sqlite3.Connection, workspace_id: str, alert: DriftAlert
) -> None:
    """Update an unresolved contract break without losing its original baseline."""
    existing = conn.execute(
        """
        SELECT id, old_signature FROM drift_alerts
        WHERE workspace_id = ? AND edited_path = ? AND symbol_name = ? AND resolved = 0
        ORDER BY id DESC LIMIT 1
        """,
        (workspace_id, alert.edited_path, alert.symbol_name),
    ).fetchone()
    if existing is not None:
        alert.old_signature = existing["old_signature"]
        conn.execute(
            """
            UPDATE drift_alerts
            SET severity = ?, new_signature = ?, reason = ?, affected_callers_json = ?
            WHERE id = ?
            """,
            (
                alert.severity, alert.new_signature, alert.reason,
                json.dumps(alert.affected_callers), int(existing["id"]),
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO drift_alerts (
            workspace_id, created_at, edited_path, symbol_name, severity,
            old_signature, new_signature, reason, affected_callers_json, resolved
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            workspace_id, time.time(), alert.edited_path, alert.symbol_name,
            alert.severity, alert.old_signature, alert.new_signature, alert.reason,
            json.dumps(alert.affected_callers),
        ),
    )


def reconcile_drift_alerts(
    conn: sqlite3.Connection, workspace_id: str
) -> list[DriftAlert]:
    """Refresh open alerts against the current graph; resolve only repaired contracts.

    Run after storing edited files and relinking callers. Unknown call arity is
    retained as a warning, not interpreted as proof that a caller was repaired.
    """
    rows = conn.execute(
        "SELECT * FROM drift_alerts WHERE workspace_id = ? AND resolved = 0 ORDER BY id",
        (workspace_id,),
    ).fetchall()
    active: list[DriftAlert] = []
    for row in rows:
        symbol = conn.execute(
            """
            SELECT signature, min_args, max_args FROM symbols
            WHERE workspace_id = ? AND rel_path = ? AND qualified_name = ?
            """,
            (workspace_id, row["edited_path"], row["symbol_name"]),
        ).fetchone()
        callers = _external_callers_for_symbol(
            conn, workspace_id, row["edited_path"], row["symbol_name"].split(".")[-1],
            include_imports=symbol is None,
        )
        severity = "critical"
        new_signature = "<removed>"
        if symbol is None:
            affected = callers
            reason = (
                f"Exported symbol '{row['symbol_name']}' was removed, "
                f"but {len(affected)} external site(s) still reference it."
            )
        else:
            new_signature = symbol["signature"]
            affected = [
                caller for caller in callers
                if caller["arg_count"] >= 0 and not _accepts_arg_count(
                    int(symbol["min_args"]), int(symbol["max_args"]), caller["arg_count"]
                )
            ]
            reason = (
                f"Signature of '{row['symbol_name']}' accepts "
                f"{symbol['min_args']}..{'*' if symbol['max_args'] < 0 else symbol['max_args']} arg(s); "
                f"{len(affected)} external call site(s) still pass incompatible argument counts."
            )
            if not affected and new_signature != row["old_signature"]:
                affected = [caller for caller in callers if caller["arg_count"] < 0]
                severity = "warning"
                reason = (
                    f"Signature of '{row['symbol_name']}' changed; "
                    f"verify {len(affected)} external call site(s) with unknown argument counts."
                )

        if not affected:
            conn.execute("UPDATE drift_alerts SET resolved = 1 WHERE id = ?", (int(row["id"]),))
            continue
        alert = DriftAlert(
            edited_path=row["edited_path"], symbol_name=row["symbol_name"],
            severity=severity, old_signature=row["old_signature"],
            new_signature=new_signature, reason=reason, affected_callers=affected[:25],
        )
        conn.execute(
            """
            UPDATE drift_alerts
            SET severity = ?, new_signature = ?, reason = ?, affected_callers_json = ?
            WHERE id = ?
            """,
            (severity, new_signature, reason, json.dumps(alert.affected_callers), int(row["id"])),
        )
        active.append(alert)
    return active


def detect_and_record_drift(
    conn: sqlite3.Connection,
    workspace_id: str,
    edited_path: str,
    old_symbols: dict[str, ParsedSymbol],
    new_symbols: dict[str, ParsedSymbol],
) -> list[DriftAlert]:
    """Compare old vs. new symbol definitions for *edited_path* and persist open drift alerts."""
    alerts: list[DriftAlert] = []

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
                    affected_callers=callers[:25],
                )
            )

    # 2. Modified signatures where arity or parameter contract breaks external callers
    for qname, new_sym in new_symbols.items():
        old_sym = old_symbols.get(qname)
        if old_sym is None:
            continue
        if old_sym.min_args == new_sym.min_args and old_sym.max_args == new_sym.max_args:
            continue

        callers = _external_callers_for_symbol(
            conn, workspace_id, edited_path, new_sym.name, include_imports=False
        )
        if not callers:
            continue

        broken_arity_callers = [
            c
            for c in callers
            if c["arg_count"] >= 0
            and _accepts_arg_count(old_sym.min_args, old_sym.max_args, c["arg_count"])
            and not _accepts_arg_count(new_sym.min_args, new_sym.max_args, c["arg_count"])
        ]
        uncertain_callers = [c for c in callers if c["arg_count"] < 0]
        contract_narrowed = new_sym.min_args > old_sym.min_args or (
            new_sym.max_args >= 0 and (old_sym.max_args < 0 or new_sym.max_args < old_sym.max_args)
        )

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
                    affected_callers=broken_arity_callers[:25],
                )
            )
        elif uncertain_callers and contract_narrowed:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=new_sym.qualified_name,
                    severity="warning",
                    old_signature=old_sym.signature,
                    new_signature=new_sym.signature,
                    reason=(
                        f"Accepted argument range of '{new_sym.qualified_name}' narrowed; "
                        f"verify {len(uncertain_callers)} external caller(s) with unknown argument counts."
                    ),
                    affected_callers=uncertain_callers[:25],
                )
            )

    for a in alerts:
        _record_alert(conn, workspace_id, a)
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
