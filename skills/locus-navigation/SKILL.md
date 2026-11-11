---
name: locus-navigation
description: Navigate unfamiliar repositories, compute pre-refactor transitive blast radius, verify covering tests, and resolve signature drift using Locus graph tools and @sym:/@impact:/@hotspots: context references.
---

# Locus Architectural Navigation & Safe Refactoring Workflow

Locus maintains a live, incremental AST & structural dependency graph of the active workspace (Python, TypeScript/JavaScript, Go, and Rust) stored in Hermes's profile-scoped SQLite store.

## When to Use Each Capability

1. **Orientation on an Unfamiliar Codebase (`locus_map`)**
   - Before grepping blindly across dozens of files, call `locus_map` (optionally scoped to a subdirectory with `scope_prefix`).
   - Locus ranks modules by **PageRank architectural centrality**, surfacing core hub modules, exported class/function signatures, and any circular import chains first.

2. **Tracing Definitions, Callers & Callees (`locus_symbol` or `@sym:<name>`)**
   - Call `locus_symbol` with a function, class, or method name to inspect its exact file/line span, signature, incoming workspace call sites (with argument counts), outgoing calls, and bounded source excerpt.
   - Users can also inline symbol neighborhoods directly in chat prompts via `@sym:SymbolName`.

3. **Pre-Refactor Blast-Radius Check (`locus_impact` or `@impact:<file-or-symbol>`)**
   - **Always** run `locus_impact` before changing an exported function signature, renaming a public symbol, or restructuring a shared module.
   - Review:
     - **Direct Dependents (Hop 1)** and **Transitive Blast Radius (Hops 2-3)**
     - **External Call Sites** with passed argument counts
     - **Covering Test Files** to execute immediately after editing
     - **Risk Level** (`LOW` / `MEDIUM` / `HIGH` / `CRITICAL`)

4. **Architectural Health & Tech-Debt Triage (`locus_hotspots` or `@hotspots:all`)**
   - Call `locus_hotspots` when asked to audit code quality, break import cycles, identify tightly coupled god-modules, find untested core files, or list unresolved signature drift alerts.

## Reacting to `[Locus Signature Drift Alert]`

Whenever `write_file` or `patch` modifies an exported symbol's required/maximum parameter count or deletes an exported symbol that external files still call, Locus automatically appends a `[Locus Signature Drift Alert]` block to the tool result naming every broken caller (`file:line` and argument count).

When you see `[Locus Signature Drift Alert]`:
1. Inspect each listed external call site immediately.
2. Update the callers to match the new contract (or restore backward-compatible defaults) in the same turn.
3. Run the covering test files identified by `locus_impact`.
