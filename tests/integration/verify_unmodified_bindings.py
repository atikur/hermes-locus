#!/usr/bin/env python3
"""Bind every statically proven call in an unmodified repository against its current signature.

Unedited code should contain no incompatible proven call: any result other than a
deliberate negative test is a binder false positive. No model calls; nothing is edited.

    python verify_unmodified_bindings.py <repository> <scratch-database> <output.json>
"""
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from locus_core import index_workspace  # noqa: E402
from locus_core.parsers import python_call_compatibility  # noqa: E402

root, db = sys.argv[1], sys.argv[2]
t = time.perf_counter()
stats = index_workspace(root, force=True, max_files=20000, db_path=db)
print("index:", {k: stats.get(k) for k in ("ok", "file_count", "symbol_count", "edge_count")}, f"{time.perf_counter()-t:.1f}s")
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
rows = conn.execute("""
  SELECT c.caller_path, c.line_no, c.callee_qualname, c.resolved_path, c.python_call_json, s.kind, s.python_signature_json
  FROM calls c LEFT JOIN symbols s ON s.workspace_id = c.workspace_id AND s.rel_path = c.resolved_path AND s.qualified_name = c.callee_qualname
  WHERE c.callee_qualname != '' AND c.resolved_path != ''""").fetchall()
tally, bad = collections.Counter(), []
for r in rows:
    shape = json.loads(r["python_call_json"])
    where = "same-file" if shape.get("import_kind") == "local" else "cross-file"
    if shape.get("receiver") == "instance": where += " instance"
    kind = r["kind"] or "no-symbol"
    group = "function" if kind in ("function", "async_function") else kind
    sig = json.loads(r["python_signature_json"]) if r["python_signature_json"] else None
    ok, why = python_call_compatibility(sig, shape)
    tally[(where, group, "proven" if sig else "linked-only", {True: "compatible", False: "INCOMPATIBLE", None: "uncertain"}[ok])] += 1
    if ok is False: bad.append((where, group, r["caller_path"], r["line_no"], r["callee_qualname"], why))
for key in sorted(tally): print(f"  {key}: {tally[key]}")
print("incompatible proven calls:", len(bad))
for b in bad[:25]: print("   ", b)
json.dump({"tally": {" / ".join(k): v for k, v in tally.items()}, "incompatible": bad, "stats": {k: stats.get(k) for k in ("file_count", "symbol_count", "edge_count")}}, open(sys.argv[3], "w"), indent=1)
