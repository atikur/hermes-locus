# Locus Codegraph — Rehearse refactors and catch broken callers in Hermes

![Locus banner](assets/locus-banner.svg)

**Locus lets Hermes check a Python signature change before and after making it.** Ask `@whatif:verify_token(token,*,scope=None)` and Locus binds every statically proven caller against the proposed signature, before any file changes, and lists the call sites that would break and why. When Hermes then edits the code, the same check runs inside the editing tool's own result: broken callers arrive with file, line and the binding error, and for a renamed keyword, a ready caller patch.

[![The 70-second live demo: rehearsal in the Hermes terminal](assets/locus-v020-demo-poster.jpg)](assets/locus-v020-demo.mp4)

[Watch the 70-second demo](assets/locus-v020-demo.mp4): one real Hermes web-chat session with gpt-5.4-mini and Locus installed from this repository. Typing and the agent's work are sped up 2x and marked on screen. [How it was recorded](docs/demo.md).

The engine uses only Python's standard library (`ast`, `sqlite3`). It needs no API key, hosted index or extra model call, and it never edits your code.

## What it checks

Python calls Locus can prove statically: functions, class constructors, static and class methods, methods on locally constructed instances (`client = Client(...)` then `client.send(...)`), and calls inside the edited file. Calls may go through direct or module-qualified imports, aliases, relative imports and static re-exports. Receivers whose type is not in view (`self`, parameters, attributes), decorated callables and dynamic bindings are not checked, and Locus never guesses a target from a matching name. [Full limits](#analysis-depth-and-limits).

## Evidence at a glance

For version 0.2.0. [Full record, method and raw files](docs/verification.md#version-020-rehearsal-same-file-and-instance-calls).

- **CI:** all 8 jobs pass on GitHub: Python 3.10 to 3.14 on Linux, macOS and Windows; the shipped UI contracts; and a job that installs this exact commit into a pinned Hermes and replays a real edit through its hooks.
- **Tests:** 146 pass in the Hermes runtime.
- **Admission:** `hermes plugins validate --install-deps` passes with zero warnings; `plugins doctor --ci` registers 5 tools and 3 hooks.
- **Pinned real source:** 45 of 45 and 24 of 24 incompatible call instances named across Hermes, HTTPX and Requests edits, and none of 65 compatible ones.
- **Classes:** 25 of 26 on pinned Hermes classes and 18 of 21 in executable fixtures, none of 51 compatible named; the misses are documented gaps.
- **False positives:** of 230,467 statically proven calls in an unmodified 17,025-file Hermes checkout, the only incompatible one is a test that expects `TypeError`.
- **With a model:** in an earlier paired study (11 pairs, version 0.1.5), warnings cut searches from 67 to 32; it did not show a clear success-rate gain (11/11 against 10/11).

These are selected cases and one large repository, not an estimate of accuracy everywhere.

## One refactor, a complete feedback loop

Suppose checkout code calls `verify_token(token, audience="checkout")`, and an API route depends on checkout.

1. **See the impact.** `@impact:auth/tokens.py` identifies direct and indirect dependents and suggests related tests.
2. **Rehearse it.** `@whatif:verify_token(token,*,scope=None)` reports, before anything is edited, that the checkout call would break with `unexpected keyword argument 'audience'`, and offers a caller patch for the rename.

   ![The rehearsal in the Hermes terminal](assets/locus-v020-whatif.jpg)

3. **Make the change.** Hermes renames the `audience` parameter to `scope`. The total number of parameters stays the same.
4. **Catch a missed caller.** Locus appends a warning to the editing tool's result.

   ```text
   [Locus Signature Drift Alert]
   CRITICAL: verify_token in auth/tokens.py
   -> Update caller services/checkout.py:6: unexpected keyword argument 'audience'
      Review replacing audience= with scope=.
   ```

5. **Repair and verify.** Hermes applies the caller patch carried in the alert, or updates the caller itself, and runs the relevant tests. Locus re-evaluates the tracked mismatch as files are indexed and clears it when the incompatible references are gone.

Locus supplies the evidence and the agent performs the repair. The warning above is abbreviated for illustration.

## Install

The plugin requires **Hermes Agent >=0.22.0** and uses Hermes's Python runtime. The standalone engine needs Python 3.10 or later.

```bash
hermes plugins install https://github.com/atikur/hermes-locus --enable
```

From the root of a Python project, run `hermes locus map` to check the installation without a configured model. Start a fresh Hermes session in that project to use Locus during edits. `/locus reindex` refreshes its graph.

Once the catalog entry is accepted, the short form is `hermes plugins install locus-codegraph --enable`. In Hermes Desktop, use the plugin installer or this deep link:

```text
hermes://plugin/install?repo=atikur/hermes-locus&enable=1
```

In the tested Desktop 0.22 build, plugin API discovery and enablement use the
backend's launch profile. A pane installed only in another selected profile can
render while its API returns `Plugin not found`. Install and enable Locus in the
profile that launched the backend as well. This is a host installation limitation,
not evidence that named-profile workspace data is shared.

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
| `@whatif:<symbol>(<new parameters>)` | A rehearsal of that signature change: which proven callers would break and why, and a caller patch for a renamed keyword. Write it without spaces, or quote it |
| `@hotspots:all` | Dependency cycles, highly connected modules, modules without inferred test links, and open drift |
| `@hotspots:cycles`, `@hotspots:drift`, `@hotspots:untested` | A focused subset of the health report |

When a symbol name is ambiguous, use its exact target, such as `@sym:auth/tokens.py::verify_token` or `@impact:auth/tokens.py::verify_token`. Locus returns candidates instead of choosing an arbitrary same-name definition.

The same analysis is available through commands:

```text
/locus
/locus map src/auth
/locus impact auth/tokens.py
/locus symbol verify_token
/locus whatif verify_token(token, *, scope=None)
/locus hotspots
/locus reindex
```

```bash
hermes locus scan
hermes locus map
hermes locus impact auth/tokens.py
hermes locus symbol verify_token
hermes locus whatif "verify_token(token, *, scope=None)"
hermes locus hotspots --json
# Analyze a specific project:
hermes locus map --workspace /path/to/project
```

## Agent tools

The `locus-codegraph` toolset exposes five tools, plus a bundled `locus-navigation` skill. Tools read source and maintain the local index; they do not modify source files.

| Tool | Parameters | Result |
| --- | --- | --- |
| `locus_map` | `scope_prefix?`, `max_files?`, `workspace?` | PageRank-ranked modules, exported signatures, languages, cycles, and drift counts |
| `locus_symbol` | `name`, `workspace?` | Definition, signature, indexed call sites, outgoing calls, and source excerpt |
| `locus_impact` | `target`, `max_depth?`, `workspace?` | Reverse dependency traversal, related test files, and a heuristic 0–100 impact score |
| `locus_rehearse` | `target`, `new_signature`, `workspace?` | Before any edit: proven callers a proposed signature would break, with reasons, and a caller patch for a renamed keyword |
| `locus_hotspots` | `workspace?` | Dependency cycles, high-coupling modules, inferred test gaps, and unresolved drift |

## Web and desktop

![The Locus tab in the Hermes Web Dashboard after the demo session](assets/locus-v020-dashboard.jpg)

*The Locus tab in the Hermes Web Dashboard at the end of the recorded demo: no open alerts, one closed after the repair, and the dependency map of the demo project.*

- **Web Dashboard:** `/locus-codegraph` opens with caller alerts: every stored location in each displayed alert, binding reasons, available repair suggestions, and copyable repair context. A keyboard-accessible graph, file list, symbol search, and workspace selection provide navigation. The page and `sessions:top` drift banner refresh every five seconds.
- **Hermes Desktop:** the **Locus Codegraph** pane shares the caller inbox and coverage warnings, provides status and command-palette actions, and refreshes through polling and plugin events. It inserts repair context or `@impact:<file>` into a draft for review; it does not send the message. [Actual Desktop 0.22 interaction checks](docs/evidence/v015-native-verification.md) covered eight callers, the complete unsent draft, keyboard navigation, repair, parsing errors, and dismissal.

The inbox shows the newest 20 open alerts with an exact total and the newest 10 closed records. Earlier open alerts appear as displayed ones clear. Closed records include automatic clearance and manual dismissal; neither means tests passed. Check the workspace in copied repair context when switching repositories.

## Analysis depth and limits

| Language | Analysis |
| --- | --- |
| Python (`.py`, `.pyi`) | AST definitions, signatures, imports, and call sites. Checks positional and keyword binding for undecorated functions, class constructors, static/class methods called through the class, and methods on locally constructed instances, across files and inside the edited file. |
| TypeScript / JavaScript | Structural definitions and imports, plus approximate call extraction; call argument counts are not available |
| Go / Rust | Structural definitions and imports; no call-site extraction |

Locus is a navigation and refactoring aid. Its results have these boundaries:

- **Supported Python bindings:** direct imports, module-qualified calls, `import`/`from` aliases, relative imports, common `src`/`lib` layouts, and static re-exports can resolve to indexed undecorated functions. Function-local imports are scoped separately. Binding checks cover renamed keywords, required arguments, and positional-only and keyword-only parameters.
- **Classes:** calling an imported class is checked against its own `__init__`, or a sole same-module base's when it defines none. `Class.method()` is checked for static and class methods, including one inherited from a sole same-module base. This applies to bare top-level classes whose bases are absent, `object`, `ABC`, built-in exceptions, or such classes in the same module.
- **Instances and same-file calls:** a method call is checked when its receiver is a name assigned exactly once from a proven constructor, such as `client = Client(...)` followed by `client.send(...)`. Calls inside the edited file are checked when they provably name that module's own function or class.
- **Repair patches:** for a single renamed keyword, the rehearsal and the post-edit alert include a patch that renames it at the proven call sites. Locus offers the patch; it never applies it.
- **Uncertain calls:** conditional or ambiguous imports, shadowed names, dynamic dispatch, assignment-created function aliases, decorated functions, and unpacked arguments may remain unresolved or require manual review. Receivers whose exact type is not in view are not checked: parameters, `self`, attributes such as `self.client`, reassigned names, and values returned by other calls. Decorated classes (including dataclasses), classes with a metaclass, `__new__`, or an imported base, and rebound or conditionally defined members have no proven calling convention. Locus does not guess a target from a matching name. No alert, and no predicted break in a rehearsal, does not establish compatibility. Runtime tests still check values and behavior.
- **Suggested tests:** the reports' “covering tests” are inferred from dependencies and filename conventions. Locus does not measure execution coverage or run those tests.
- **Risk scores:** the 0–100 score combines dependency counts, inferred test gaps, and drift alerts. It is a prioritization heuristic, not a failure probability.
- **Edit feedback:** hooks respond to supported Hermes editing tools and recognized paths. Locus is not a filesystem watcher; after external editor or shell changes, use `/locus reindex` to refresh the graph.
- **Indexing:** supported source files are bounded by size and file count. Root-level `.gitignore` patterns are supported on a best-effort basis; nested ignore files and negation are not implemented.
- **Incomplete edits:** invalid Python retains the last valid graph and produces an indexing warning. Repair the syntax and reindex before treating that file's graph as current. Parse failures and file-cap warnings persist across page refreshes and appear in analysis reports.
- **Performance:** unchanged files are cached using modification time, size, and SHA-256. An edited file is reparsed, then dependency links and rankings are refreshed. Timings depend on workspace size and hardware; the demo prints measurements for its own small fixture.

## Configuration

Use Hermes Settings or `plugins.entries.locus-codegraph.settings` in your Hermes configuration.

| Setting | Default | Effect |
| --- | --- | --- |
| `auto_index_on_start` | `true` | Incrementally index the active workspace when a session starts |
| `drift_guard` | `true` | Append signature-drift notices to recognized editing-tool results |
| `max_files` | `2500` | Maximum files for session-start scans and `/locus reindex`; set a positive integer |

Web/Desktop rescans reuse the last recorded scan cap, so refreshing a workspace does not silently replace a larger configured scan with the default.

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
node --test tests/ui.test.cjs
```

FastAPI and HTTPX are needed to test the dashboard API outside Hermes. The core itself has no third-party runtime dependencies. The Node tests load the shipped bundles with SDK component stubs; they verify UI contracts, not native rendering. Tests that require Hermes's own modules are skipped in standalone mode; run the suite in a configured Hermes environment as well to exercise those integrations.

Before publishing, use the actual Hermes environment to run:

```bash
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

See [the submission runbook](docs/submission.md) for the real-session demonstration and evidence checklist.

In one final-engine v0.1.5 trial, a full 17,025-file index took 65.50 seconds from a fresh database and 1.98 seconds cached. A real Hermes one-file patch with caller feedback took 6.76 seconds; a three-file repair took 6.51 seconds. These include host work and are not isolated plugin overhead. The scan used a raised 20,000-file cap. [Exact snapshot, method, limits, and historical measurements](docs/performance.md).

## Verified behavior

Version 0.2.0 is summarised in [Evidence at a glance](#evidence-at-a-glance) and recorded in full in [the verification record](docs/verification.md#version-020-rehearsal-same-file-and-instance-calls). Earlier versions keep their own sections there:

- **0.1.5:** actual Web and native Desktop 0.22 interaction checks on a synthetic fixture, including a [stale-source follow-up](docs/evidence/v015-native-stale-verification.md); a [performance trial](docs/performance.md); and the [paired model study](docs/verification.md#paired-model-study-on-the-frozen-engine), with all 24 attempts preserved.
- **0.1.4:** a [recorded refactor on Hermes Agent's own code](docs/verification.md#live-keyword-refactor-on-hermes-agent) in which Locus named all five affected call sites in the first edit's result, before Hermes searched for callers.
- **Earlier demos:** the [60-second study replay](assets/locus-v015-proof.mp4) for 0.1.5 and the [77-second model session](https://github.com/atikur/hermes-locus/releases/download/v0.1.2/locus-submission-demo.mp4) for 0.1.2.

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
| `locus_core/rehearsal.py` | Signature rehearsal and caller patches |
| `dashboard/`, `desktop/` | Web and native Hermes interfaces |
| `tests/`, `scripts/run_demo.py` | Automated checks and deterministic engine showcase |

## Privacy and behavior disclosure

- **Reads:** supported source files inside the selected workspace. The scanner rejects filesystem/home/system roots and sensitive credential directories, skips hidden files and build/vendor directories, rejects symlink escapes, skips detected binary content, and limits each source file to 512 KiB. Supported extensions are `.py`, `.pyi`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.go`, and `.rs`.
- **Stores locally:** file metadata, signatures, short docstring summaries, imports, calls, drift records, and activity metadata in `<HERMES_HOME>/plugin-data/locus-codegraph/locus.db`. Full source files are not stored in the database. Source excerpts are read when requested.
- **Changes context:** explicit `@sym:`, `@impact:`, and `@hotspots:` references expand into reports. With `drift_guard` enabled, recognized editing-tool results may include a drift notice. Those reports and source excerpts become visible to the model configured in Hermes.
- **No external service calls:** Locus's analysis makes no outbound network requests, reads no API credentials, collects no telemetry, and spawns no shell commands or background daemons. Web/Desktop clients communicate with Hermes's API. Locus never modifies workspace source files.

## License

MIT © Atikur Rahman
