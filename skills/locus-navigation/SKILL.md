---
name: locus-navigation
description: Navigate repositories, inspect dependencies before refactoring, select relevant tests, and respond to signature drift using Locus Codegraph tools and context references.
---

# Locus Codegraph navigation and refactoring

Locus maintains a local structural dependency graph of the selected workspace.
Python has AST definitions, signatures, and positional and keyword binding for
supported direct imports. TypeScript and
JavaScript have lighter structural and call extraction; Go and Rust have
definitions and imports. The graph is useful evidence, not a complete type checker
or execution-coverage report.

## Choose the relevant tool

1. **Orient with `locus_map`.** Inspect ranked modules, exported signatures, and
   dependency cycles. Use `scope_prefix` when the task concerns a known directory.
2. **Inspect a symbol with `locus_symbol` or `@sym:<name>`.** Read its definition,
   indexed callers and callees, and source excerpt. Check ambiguous names against
   the source before relying on a caller link.
3. **Check a change with `locus_impact` or `@impact:<file-or-symbol>`.** Before
   changing a shared signature, removing an exported symbol, or moving a module,
   review direct and transitive dependents, known call sites, and suggested tests.
   The impact score is a prioritization heuristic, not a probability of failure.
4. **Inspect health with `locus_hotspots` or `@hotspots:all`.** Use `cycles`, `drift`,
   or `untested` to focus the report. An inferred test gap does not prove there is
   no runtime test coverage.

## Respond to a drift alert

After supported editing-tool calls, Locus can append a
`[Locus Signature Drift Alert]` naming external references affected by a
signature change or symbol removal. An exact binding diagnostic can explain
which argument failed and suggest a keyword replacement to review.

When an alert appears:

1. Inspect the listed caller locations and the changed contract.
2. Update callers or restore a compatible contract, according to the user's task.
3. Run the relevant tests. “Covering tests” in a Locus report are suggestions
   inferred from dependencies and naming; select additional tests when needed.
4. Query `locus_hotspots` or `/locus drift` to inspect remaining alerts. A cleared
   alert means the tracked structural mismatch was resolved, not that behavior
   has been proven correct.

The plugin does not edit source or run tests itself. Do not claim an automatic
repair unless the agent actually changed and verified the code. Do not dismiss a
warning merely to report a clean result.

## Keep the index relevant

- Work in the intended repository, or pass `workspace` explicitly.
- After external editor or shell changes, run `/locus reindex` before relying on
  the graph. Locus is not a filesystem watcher.
- Account for incomplete static resolution, dynamic dispatch, aliases, and
  unknown argument counts. Investigate the source when the report is uncertain.
- Use existing repository tests and checks to verify the final change.
