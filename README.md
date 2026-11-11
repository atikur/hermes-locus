# Locus — Catch cross-file breakage while Hermes edits

![Locus banner](assets/locus-banner.svg)

**Locus shows Hermes what a change could break, then flags incompatible callers while it works.** It builds a local structural graph of your repository and connects it to agent tools, `@` context references, the Web Dashboard, and Hermes Desktop.

The analysis engine uses Python's standard library (`ast`, `sqlite3`, and structural parsers). It needs no API key, hosted index, or extra model call. Hermes supplies the web and desktop integration libraries.

## One refactor, a complete feedback loop

Suppose `verify_token(token)` is called by checkout code, which is used by an API route:

1. **See the impact.** `@impact:auth/tokens.py` identifies direct and indirect dependents and suggests related tests.
2. **Make the change.** Hermes adds a required `audience` argument.
3. **Catch a missed caller.** Locus appends an actionable warning to the editing tool's result:

   ```text
   [Locus Signature Drift Alert]
   Your edit changed or removed exported symbol contract(s) with existing external callers:
   - CRITICAL: verify_token now requires 2 arguments
     -> Update caller services/checkout.py:6 (passes 1 argument)
   ```

4. **Repair and verify.** Hermes updates the caller and runs the relevant tests. Locus re-evaluates the tracked mismatch as files are indexed and clears it when the incompatible references are gone.

Locus supplies the evidence; the agent performs the repair. It never edits your workspace source files itself. The warning above is abbreviated for illustration.

## Install

Requires **Hermes Agent >=0.22.0** and **Python >=3.10**. Python 3.11 is a convenient choice for standalone development.

```bash
hermes plugins install https://github.com/atikur/hermes-locus
```

After catalog acceptance, the short form is `hermes plugins install locus`. In Hermes Desktop, use the plugin installer or this deep link:

```text
hermes://plugin/install?repo=atikur/hermes-locus&enable=1
```

Start Hermes in the repository you want to analyze. Session startup indexes that workspace by default; `/locus reindex` refreshes it explicitly.

## Try it

In chat:

```text
Inspect @sym:verify_token and @impact:auth/tokens.py before adding a required audience argument.
```

```text
Review @hotspots:cycles and explain the smallest change that would break a dependency cycle.
```

| Reference | What it adds to the prompt |
| --- | --- |
| `@sym:<name>` | Definition and signature, indexed callers/callees, and a bounded source excerpt |
| `@impact:<file-or-symbol>` | Direct and transitive dependents, suggested tests, risk score, and open drift alerts |
| `@hotspots:all` | Dependency cycles, highly connected modules, modules without inferred test links, and open drift |
| `@hotspots:cycles`, `@hotspots:drift`, `@hotspots:untested` | A focused subset of the health report |

The same analysis is available through commands:

```text
/locus
/locus map src/auth
/locus impact auth/tokens.py
/locus symbol verify_token
/locus hotspots
/locus reindex
```

```bash
hermes locus scan
hermes locus map
hermes locus impact auth/tokens.py
hermes locus symbol verify_token
hermes locus hotspots --json
# Analyze a specific project:
hermes locus map --workspace /path/to/project
```

## Agent tools

The `locus` toolset exposes four tools, plus a bundled `locus-navigation` skill. Tools read source and maintain the local index; they do not modify source files.

| Tool | Parameters | Result |
| --- | --- | --- |
| `locus_map` | `scope_prefix?`, `max_files?`, `workspace?` | PageRank-ranked modules, exported signatures, languages, cycles, and drift counts |
| `locus_symbol` | `name`, `workspace?` | Definition, signature, indexed call sites, outgoing calls, and source excerpt |
| `locus_impact` | `target`, `max_depth?`, `workspace?` | Reverse dependency traversal, related test files, and a heuristic 0–100 impact score |
| `locus_hotspots` | `workspace?` | Dependency cycles, high-coupling modules, inferred test gaps, and unresolved drift |

## Web and desktop

![Locus running in the Hermes Web Dashboard](assets/locus-live-dashboard.jpg)

*Captured from the real Hermes Web Dashboard using the disposable checkout fixture.*

- **Web Dashboard:** `/locus` provides a selectable dependency graph, impact rings, symbol search, workspace selection, and drift inspection. The page and `sessions:top` drift banner refresh every five seconds.
- **Hermes Desktop:** the **Locus Graph** pane shares the analysis API, provides status and command-palette actions, refreshes through polling and plugin events, and can insert `@impact:<file>` into the composer.

Check the selected workspace when switching between repositories. Dismissing a warning in the UI acknowledges it; dismissal does not repair code or establish that tests pass.

## Analysis depth and limits

| Language | Analysis |
| --- | --- |
| Python (`.py`, `.pyi`) | AST definitions, signatures, imports, call sites, and argument-count checks |
| TypeScript / JavaScript | Structural definitions and imports, plus approximate call extraction; call argument counts are not available |
| Go / Rust | Structural definitions and imports; no call-site extraction |

Locus is a navigation and refactoring aid. Its results have these boundaries:

- **Static resolution:** Python calls are linked using names and imports. Dynamic dispatch, runtime imports, aliases, and ambiguous names can produce missing or approximate links. Argument-count checks do not prove keyword binding, type compatibility, or behavioral correctness.
- **Suggested tests:** the reports' “covering tests” are inferred from dependencies and filename conventions. Locus does not measure execution coverage or run those tests.
- **Risk scores:** the 0–100 score combines dependency counts, inferred test gaps, and drift alerts. It is a prioritization heuristic, not a failure probability.
- **Edit feedback:** hooks respond to supported Hermes editing tools and recognized paths. Locus is not a filesystem watcher; after external editor or shell changes, use `/locus reindex` to refresh the graph.
- **Indexing:** supported source files are bounded by size and file count. Root-level `.gitignore` patterns are supported on a best-effort basis; nested ignore files and negation are not implemented.
- **Performance:** unchanged files are cached using modification time, size, and SHA-256. An edited file is reparsed, then dependency links and rankings are refreshed. Timings depend on workspace size and hardware; the demo prints measurements for its own small fixture.

## Configuration

Use Hermes Settings or `plugins.config.locus` in your Hermes configuration:

| Setting | Default | Effect |
| --- | --- | --- |
| `auto_index_on_start` | `true` | Incrementally index the active workspace when a session starts |
| `drift_guard` | `true` | Append signature-drift notices to recognized editing-tool results |
| `max_files` | `2500` | Maximum files for session-start scans and `/locus reindex`; set a positive integer |

## Development and verification

The standalone engine demo needs only Python >=3.10:

```bash
python3.11 scripts/run_demo.py
python3.11 scripts/run_demo.py --cinematic
```

This fixture exercises indexing, impact, drift detection, and clearance. It simulates edits directly; it is not a recording of an LLM-driven Hermes session.

For a real Hermes session, create a persistent disposable project in an empty directory:

```bash
python3.11 scripts/create_demo_workspace.py ../locus-demo
cd ../locus-demo
python3.11 -m unittest discover -s tests -v
hermes
```

The fixture contains synthetic authentication logic, two direct callers, an API layer, and three baseline tests. Use the prompt in [the submission runbook](docs/submission.md) to demonstrate the edit-feedback-repair loop. A configured Hermes model is needed for the agent session; creating and testing the fixture needs no API key.

Install the test-only dependencies in an isolated environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

FastAPI and HTTPX are needed to test the dashboard API outside Hermes. The core itself has no third-party runtime dependencies. Tests that require Hermes's own modules are skipped in standalone mode; run the suite in a configured Hermes environment as well to exercise those integrations.

Before publishing, use the actual Hermes environment to run:

```bash
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

See [the submission runbook](docs/submission.md) for the real-session demonstration and evidence checklist.

## Verified behavior

Version 0.1.1 passed 41 tests in the Hermes environment. A real GPT-5.4 mini session received the drift warning, repaired both callers, and passed the fixture tests. See [verification evidence and the full transcript](docs/verification.md) for commands, results, and limits.

## Architecture

```text
Workspace source
  → Python AST / structural parsers
  → incremental index + SQLite WAL
  → resolved dependencies + PageRank + cycle detection
  → agent tools / context references / web API / desktop pane

Hermes editing tool
  → editing hooks: reindex and compare contracts
  → tool result: append actionable drift feedback
  → agent repairs callers and runs tests
```

| Path | Responsibility |
| --- | --- |
| `plugin.yaml`, `__init__.py` | Hermes manifest, registration, commands, tools, context providers, and hooks |
| `locus_core/parsers.py`, `indexer.py` | Source extraction, incremental scanning, and dependency linking |
| `locus_core/storage.py` | Profile-scoped SQLite storage |
| `locus_core/graph.py` | Repository maps, symbols, impact, cycles, and visualization data |
| `locus_core/drift.py`, `formatter.py` | Contract comparison and bounded reports |
| `dashboard/`, `desktop/` | Web and native Hermes interfaces |
| `tests/`, `scripts/run_demo.py` | Automated checks and deterministic engine showcase |

## Privacy and behavior disclosure

- **Reads:** supported source files inside the selected workspace. The scanner rejects filesystem/home/system roots and sensitive credential directories, skips hidden files and build/vendor directories, rejects symlink escapes, skips detected binary content, and limits each source file to 512 KiB. Supported extensions are `.py`, `.pyi`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.go`, and `.rs`.
- **Stores locally:** file metadata, signatures, short docstring summaries, imports, calls, drift records, and activity metadata in `<HERMES_HOME>/plugin-data/locus/locus.db`. Full source files are not stored in the database. Source excerpts are read when requested.
- **Changes context:** explicit `@sym:`, `@impact:`, and `@hotspots:` references expand into reports. With `drift_guard` enabled, recognized editing-tool results may include a drift notice. Those reports and source excerpts become visible to the model configured in Hermes.
- **No external service calls:** Locus's analysis makes no outbound network requests, reads no API credentials, collects no telemetry, and spawns no shell commands or background daemons. Web/Desktop clients communicate with Hermes's API. Locus never modifies workspace source files.

## License

MIT © Atikur Rahman
