#!/usr/bin/env python3
"""Compare two relink implementations against copies of one completed index.

Use a SQLite database with one workspace and no pending WAL writes. For a new
seed, index a disposable archive of the pinned Hermes workspace with
``index_workspace(workspace, max_files=20000, db_path=seed)``. The benchmark
never imports the indexed source, mutates the input database, or calls a model.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path


SOURCE_COMMIT = "78abaafcccec20a546c94e8734a70e38c08a6a69"
BASELINE = "abd696671431cf0d99694354019321fd3d6826b0"
OPTIMIZED = "fcdb087e45c93bacca529bbd087014e84033ad8c"
EXTRA_SOURCE = b'from agent.display import get_tool_emoji\n\ndef report_tool():\n    return get_tool_emoji("patch")\n'


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def load_archived_indexer(repo: Path, revision: str, target: Path, name: str):
    target.mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(repo), "archive", revision, "locus_core"], stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tree:
            tree.extractall(target, filter="data")
    package = target / "locus_core"
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py", submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return sys.modules[f"{name}.indexer"]


def graph_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for query in (
        "SELECT rel_path, pagerank, fan_in, fan_out FROM files ORDER BY rel_path",
        "SELECT src_path, target_spec, resolved_path FROM imports ORDER BY rowid",
        "SELECT caller_path, callee_name, resolved_path FROM calls ORDER BY rowid",
    ):
        for row in conn.execute(query):
            digest.update(repr(tuple(row)).encode("utf-8") + b"\n")
    return digest.hexdigest()


def clone_index(seed: Path, target: Path) -> None:
    if platform.system() == "Darwin":
        result = subprocess.run(["/bin/cp", "-c", str(seed), str(target)], capture_output=True)
        if result.returncode == 0:
            return
    shutil.copyfile(seed, target)


def measure(indexer, seed: Path, db: Path, helper: bytes) -> list[dict]:
    clone_index(seed, db)
    results = []
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        [workspace] = conn.execute("SELECT workspace_id FROM workspaces").fetchall()
        workspace_id = workspace["workspace_id"]
        for name, path, source in (
            ("reparse-display", "agent/display.py", helper),
            ("unchanged", None, None),
            ("add-dependency", "rank_bench_example.py", EXTRA_SOURCE),
        ):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("BEGIN IMMEDIATE")
            if source is not None:
                parsed = indexer.parse_source_file(path, source.decode("utf-8"))
                assert parsed is not None and not parsed.parse_error
                indexer._store_parsed_file(conn, workspace_id, parsed, 0, len(source), hashlib.sha256(source).hexdigest())
            changes = conn.total_changes
            start = time.perf_counter()
            edges = indexer._relink_and_rank(conn, workspace_id)
            conn.commit()
            elapsed = time.perf_counter() - start
            results.append({
                "scenario": name, "relink_seconds_including_commit": round(elapsed, 4),
                "sql_rows_updated_during_relink": conn.total_changes - changes,
                "wal_bytes_including_reparse": db.with_name(db.name + "-wal").stat().st_size,
                "edges": edges, "graph_sha256": graph_digest(conn),
            })
    db.unlink()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Hermes Git checkout containing the pinned workspace revision")
    parser.add_argument("--index", type=Path, required=True, help="completed SQLite index containing one workspace")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--optimized", default=OPTIMIZED)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    source = args.index.resolve(strict=True)
    wal = source.with_name(source.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        parser.error("use a completed index with no pending WAL writes")
    repo = Path(__file__).resolve().parents[2]
    revisions = {name: git(repo, "rev-parse", getattr(args, name)).decode().strip() for name in ("baseline", "optimized")}
    helper = git(args.source, "show", f"{SOURCE_COMMIT}:agent/display.py")
    report = {
        "revisions": revisions, "workspace_source_commit": SOURCE_COMMIT,
        "environment": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "python_hash_seed": os.environ.get("PYTHONHASHSEED")},
        "method": "SQLite backup to one seed; each arm starts from a copy of the same seed. Arm order alternates by repeat.",
        "limits": ["One workspace on one machine; cache and disk effects still vary by order.",
                   "Wall time covers relink and commit. It excludes scanning, reparsing, copying, and graph comparison.",
                   "WAL size includes any staged reparse writes, while SQL update counts cover relink only."],
        "trials": [],
    }
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="locus-relink-bench-") as folder:
        scratch = Path(folder)
        seed = scratch / "seed.db"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as existing, \
             closing(sqlite3.connect(seed)) as backup:
            existing.backup(backup)
            report["seed_rows"] = {table: backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                   for table in ("workspaces", "files", "imports", "calls", "symbols")}
        with source.open("rb") as stream:
            report["input_index_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        indexers = {name: load_archived_indexer(repo, commit, scratch / name, f"bench_{name}")
                    for name, commit in revisions.items()}
        for repeat in range(args.repeats):
            order = list(revisions) if repeat % 2 == 0 else list(reversed(revisions))
            arms = {}
            for name in order:
                arms[name] = measure(indexers[name], seed, scratch / f"{name}.db", helper)
                print(f"Repeat {repeat + 1}, {name}: {[row['relink_seconds_including_commit'] for row in arms[name]]}", flush=True)
            for baseline, optimized in zip(arms["baseline"], arms["optimized"], strict=True):
                assert (baseline["scenario"], baseline["edges"], baseline["graph_sha256"]) == (
                    optimized["scenario"], optimized["edges"], optimized["graph_sha256"],
                ), (baseline, optimized)
            report["trials"].append({"order": order, "arms": arms, "graph_equal": True})
    report["median_relink_seconds"] = {
        name: {row["scenario"]: statistics.median(
            trial["arms"][name][i]["relink_seconds_including_commit"] for trial in report["trials"]
        ) for i, row in enumerate(report["trials"][0]["arms"][name])}
        for name in revisions
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
