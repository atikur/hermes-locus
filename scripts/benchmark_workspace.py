#!/usr/bin/env python3
"""Measure a fresh index, cached scan, and reports against a chosen workspace.

Uses a disposable database, never edits the workspace, and makes no model calls.
The first scan has a fresh database; this does not flush operating-system caches.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from locus_core import (
    analyze_impact,
    build_visual_graph,
    get_hotspots_report,
    get_repo_map,
    index_workspace,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--target", help="Optional workspace-relative impact target")
    parser.add_argument("--max-files", type=int, default=2500)
    args = parser.parse_args()
    if args.max_files < 1:
        parser.error("--max-files must be positive")

    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "max_files": args.max_files,
        "method": "One fresh database scan, one cached scan, one call per report; OS caches unchanged",
        "measurements": [],
    }

    def measure(name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        start = time.perf_counter()
        result = operation()
        entry: dict[str, Any] = {"name": name, "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}
        if result.get("ok") is False:
            raise RuntimeError(result.get("error", "Analysis failed"))
        if name.endswith("scan"):
            entry["summary"] = {key: value for key, value in result.items()
                                if key not in {"workspace_id", "root_path"}}
        else:
            entry["result_bytes"] = len(json.dumps(result).encode("utf-8"))
        report["measurements"].append(entry)
        return result

    try:
        with tempfile.TemporaryDirectory(prefix="locus-benchmark-") as scratch:
            db = Path(scratch) / "locus.db"
            measure("fresh_database_scan", lambda: index_workspace(args.workspace, force=True, max_files=args.max_files, db_path=db))
            measure("cached_scan", lambda: index_workspace(args.workspace, max_files=args.max_files, db_path=db))
            measure("map", lambda: get_repo_map(args.workspace, db_path=db))
            if args.target:
                measure("impact", lambda: analyze_impact(args.workspace, args.target, db_path=db))
            measure("hotspots", lambda: get_hotspots_report(args.workspace, db_path=db))
            measure("visual_graph", lambda: build_visual_graph(args.workspace, db_path=db))
    except (OSError, RuntimeError, ValueError) as exc:
        report["error"] = str(exc)
        print(json.dumps(report, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
