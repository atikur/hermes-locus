# Locus submission runbook

## Entry description

**One sentence:** Locus shows Hermes what a change could break, then flags incompatible callers while it works.

**Short description:** Locus adds a local structural code graph to Hermes Agent. Before a refactor, `@sym:` and `@impact:` expose definitions, callers, dependent modules, and suggested tests. After supported edits, the signature-drift guard places actionable caller warnings in the tool result so Hermes can repair them. Python receives AST and argument-count analysis; TypeScript/JavaScript, Go, and Rust receive lighter structural indexing. The same index powers agent tools, commands, a Web Dashboard, and a Desktop pane. The core uses Python's standard library and requires no hosted index or additional model call.

**What makes it a plugin:** Locus persists a workspace index, subscribes to Hermes lifecycle hooks, transforms tool results, registers context-reference providers, and exposes interactive web/desktop interfaces. The distinctive workflow is structural feedback arriving inside the editing loop.

## A 70-second working demonstration

Record an actual Hermes session with Locus enabled. Keep source edits, tool feedback, and test output legible. Use a disposable Python project with a shared helper, a direct caller, an indirect caller, and a meaningful test. Do not record credentials or private project content.

| Time | Visible action | Narration |
| --- | --- | --- |
| 0–8s | Show `verify_token(token)` and a caller in another module | “Changing a shared function can leave callers behind. Locus gives Hermes structural feedback while it edits.” |
| 8–20s | Ask Hermes to inspect `@sym:verify_token` and `@impact:auth/tokens.py`; show the caller and suggested test | “Before changing it, Hermes can see the affected modules and which tests are likely relevant.” |
| 20–38s | Have Hermes add a required `audience` argument to the helper first; show the actual editing-tool result | “Locus catches this caller still passing one argument and adds its location directly to the tool result.” |
| 38–54s | Hermes updates the caller to pass the audience | “The agent uses that evidence to repair the caller.” |
| 54–65s | Run the relevant test and query open drift; show the actual output | “The test passes, and the tracked mismatch clears after the caller is repaired.” |
| 65–70s | Briefly show the graph or final repository URL | “Locus: a local code graph connected to Hermes's refactoring loop.” |

For a deterministic demonstration of the warning, explicitly tell Hermes this is a controlled demonstration and to edit the helper before updating callers. Otherwise a capable agent may update all files in one batch, leaving no intermediate breakage to show. Do not claim the demonstration measures a baseline agent's failure rate.

Suggested prompt:

```text
This disposable project is a demonstration of Locus. Inspect @sym:verify_token
and @impact:auth/tokens.py. Change verify_token to require an audience argument.
For this demonstration, edit the helper first in a separate tool call, inspect
the returned Locus feedback, then update the affected callers. Run the relevant
tests and report any remaining drift alerts.
```

The existing `scripts/run_demo.py` is a useful engine smoke test and rehearsal. It performs scripted file changes; identify it as a fixture demo if it appears in a video. A standalone script run is not evidence of an LLM repairing callers inside Hermes.

## Evidence to collect from the final commit

Record actual results below after execution. These placeholders are not pass claims.

| Evidence | Result to fill |
| --- | --- |
| Plugin commit | `[FINAL_40_CHARACTER_SHA]` |
| Hermes version and source commit | `[VERSION_AND_SHA]` |
| Python version / platform | `[VERSION_AND_PLATFORM]` |
| Standalone test suite | `[PASS_COUNT, SKIP_COUNT, COMMAND, DATE]` |
| Test suite with Hermes modules available | `[PASS_COUNT, SKIP_COUNT, COMMAND, DATE]` |
| `hermes plugins validate . --install-deps` | `[ACTUAL_RESULT]` |
| `hermes plugins doctor . --ci` | `[ACTUAL_RESULT]` |
| Catalog validator and payload checks | `[ACTUAL_RESULTS]` |
| Real Hermes edit → warning → caller repair → test | `[TRANSCRIPT_OR_RECORDING_REFERENCE]` |
| Web / desktop surfaces included in the video | `[WHAT_WAS_ACTUALLY_EXERCISED]` |

Measure performance only when needed. Include hardware, source-file count, cold versus cached scan, command, and observed elapsed time. Do not generalize the tiny fixture's timings to a large repository.

## Release and entry sequence

1. Finish the source changes and run the checks above.
2. Commit the tested plugin. Publish the repository and verify the public URL can be cloned.
3. Update the catalog entry's `sha`, asset URLs, repository URL, version, and maintainer to match that published commit. Never leave the handoff's original SHA in a changed submission.
4. Run the current catalog validator and open the catalog PR.
5. Publish the working demo video on X, under 90 seconds, tagging `@NousResearch`.
6. Submit the catalog PR and demo links through the official entry form. Confirm the form's success state and retain it.

The catalog itself requires maintainer review and merge before listing. Hackathon entry timing and form requirements are separate; check the organizer's announcement at submission time.

## Submission links

- Public repository: `[VERIFIED_PUBLIC_REPOSITORY_URL]`
- Catalog pull request: `[CATALOG_PR_URL]`
- Demo post: `[X_DEMO_URL]`
- Entry form: <https://form.typeform.com/to/FwJLvOln>
- Announcement: <https://x.com/NousResearch/status/2118094256438936062>
- Catalog rules: <https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission>

## Suggested X post

> Built Locus for the @NousResearch Hermes Agent Plugin Hackathon. It shows Hermes a change's affected callers, flags signature mismatches inside tool results, and tracks them as the agent repairs the code. Local analysis, no hosted index. Working demo below. Repo: [PUBLIC_REPO_URL]

## Catalog PR body outline

Use the final verified results, not the historical handoff's validation claims.

```markdown
## Summary

Adds Locus, pinned to [FINAL_SHA] in [PUBLIC_REPO_URL].

Locus connects a local code graph to Hermes's refactoring loop: inspect symbols
and dependent modules before edits, receive caller-mismatch feedback in tool
results, and inspect the same graph through web and desktop interfaces.

## Disclosure

- Reads supported source files in the selected workspace, with path, size,
  hidden-file, symlink, and binary guards.
- Stores structural metadata, short docstring summaries, and drift/activity
  records in Hermes's profile-scoped Locus database; does not modify source.
- Expands explicit context references and may append drift warnings to editing
  tool results. Requested reports and source excerpts enter model context.
- No external service calls, API credentials, telemetry, shell commands, or
  background daemons in the analysis engine.
- Python AST analysis is deepest; other language support is structural.

## Validation

[ACTUAL_COMMANDS_AND_RESULTS_FROM_FINAL_COMMIT]

## Demo

[WORKING_DEMO_URL]
```
