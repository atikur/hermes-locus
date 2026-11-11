# Locus Codegraph — caller feedback when Hermes edits

**Locus Codegraph puts precise caller feedback inside Hermes's edit results.**

[60-second recorded-study replay](../assets/locus-v015-proof.mp4) · [Current evidence](verification.md#version-015-candidate) · [Actual final-engine Desktop/Web checks](evidence/v015-native-stale-verification.md) · [Installation](../README.md#install)

The replay shows one selected controlled `efbf401` study session with a pre-applied helper edit and independently run tests; it is not a live UI capture, final-engine run, or typical-success estimate. [Media provenance](evidence/v015-demo-selected-evidence.json).

**Private v0.1.5 draft; not submitted.** Final-engine corpus reruns and the paired model study are complete; their snapshots and limits are distinguished below.

A Python keyword rename can break a caller without changing the number of
arguments. Locus checks supported cross-file bindings after an edit and puts
the caller's file, line, binding error, and available repair suggestion directly
in the editing tool's result. Hermes can act on that evidence before its next
search. The plugin supplies the evidence; the agent repairs code and runs tests.

One local index also powers four agent tools, `@sym:` / `@impact:` references,
and a caller inbox in Web and Desktop. Its Python standard-library engine needs
no hosted index, API key, or extra model call.

The final v0.1.5 corpus named **42/42 incompatible call instances and 0/34
compatible instances** across 19 selected Hermes, HTTPX, and Requests edits.
An additional real-source corpus found **22/24**, with both misses explicitly
retained as same-file calls. Actual Desktop and Web checks exercised eight
callers, a complete unsent repair draft, repair, and dismissal. The final
candidate passes **123 Hermes-host tests, 10 UI checks, and 7 evaluator checks**.
These are targeted checks, not a claim of general agent-success improvement;
snapshot differences and remaining limits are recorded below.

## What makes it a plugin

Locus Codegraph persists a workspace index, subscribes to Hermes lifecycle hooks,
transforms tool results, registers context-reference providers, and exposes web
and desktop interfaces. These capabilities require maintained code and host
integration beyond a prompt or skill. The product focuses on actionable
cross-file caller evidence at the moment an edit returns. The plugin supplies
evidence; Hermes performs repairs.

Python analysis supports direct imports, module-qualified calls, import aliases,
and static re-exports of undecorated functions. TypeScript, JavaScript, Go,
and Rust receive lighter structural indexing. The precise signature guard is
bounded to supported external Python callers.

## What changes in v0.1.5

- Broader static Python resolution, with explicit ambiguity instead of
  selecting an unrelated same-name definition. Same-file calls and dynamic
  bindings remain limitations.
- A caller inbox in Web and Desktop: full stored locations for each displayed
  alert, binding reasons, available suggestions, and repair context to copy or
  add to a draft. The UI distinguishes unavailable, partial, open, and closed
  states; dismissal does not assert that code was repaired.
- Persistent parse, file-limit, and stale-source warnings, exact open-alert totals, workspace
  and profile isolation, and rescans that preserve the previous scan cap.
- Additional targeted resolution controls and a paired model-study protocol
  with frozen inputs, randomized order, and independent repair checks. Results
  are reported separately from the earlier studies below.

The graph's suggested tests are inferred from imports and names, not execution
coverage. Locus does not run tests, edit source, or add its own model call. Its
analysis is local and uses Python's standard library. Reports placed in Hermes
context are visible to the model configured by the user.

## Current deterministic evidence

The final v0.1.5 engine named 42 of 42 incompatible call instances in the
19-edit Hermes/HTTPX/Requests corpus and none of 34 compatible instances.
An additional 12-case real-source corpus named 22 of 24 incompatible instances
and none of 28 compatible instances. All 22 external calls were found; the two
misses were same-file calls at one source location under two mutations. A
separate 25-case set of authored controls named 16 of 16 incompatible instances
and none of 13 compatible instances. These are targeted binding regressions,
not a representative accuracy or agent-success benchmark.
[Raw results, exact engine digest, and limits](verification.md#version-015-candidate).

The final candidate's local suite records 123 passing Python checks in Hermes, 10 UI contract
checks, and 7 evaluator-validity checks. On the minimum supported Python 3.10.22,
120 checks passed and 3 Hermes-runtime-only checks were skipped. A full 17,025-file performance trial
took 65.50 seconds for a fresh index and 1.98 seconds cached; real edit/repair
dispatches took 6.76 and 6.51 seconds, including host work. This single observation
used exact checkpoint `e71824bc` and the final `c622b20c` engine; it does not
isolate plugin overhead or establish a general speedup.
[Method and raw measurements](performance.md).
A [fresh exact-commit local Git installation](evidence/v015-install-replay.json)
also reproduced five caller warnings, three failing upstream tests after the
edit, and all 26 tests passing with zero alerts after repair. The initial
dependency-download failure is preserved separately as an environment failure.

The final candidate also preserves an established caller warning if subsequent
edits make its binding uncertain or a previously indexed source becomes
unavailable to the scanner. Stale sources remain explicit **Needs verification**
evidence rather than proving a repair. These fixes have separate regression
evidence and postdate the frozen model-study engine; final-candidate corpus
reruns reproduced the counts above. Actual Web and Desktop checks exercised all eight fixture
callers, a complete unsent repair draft, keyboard navigation, persisted parse
warnings, automatic clearance after repair and passing tests, and dismissal
while the underlying code remained broken. [Observed interaction record](evidence/v015-native-verification.md).
A [separate final-engine follow-up](evidence/v015-native-stale-verification.md)
verified persistent oversized-source warnings, retained caller evidence, a
successful recheck that kept still-broken callers open, and cleanup with passing tests.

The completed `gpt-5.4-mini` study preserved 24 attempts on four selected tasks
with three repeats: 23 completed and one privacy interruption. The 11 complete
pairs produced ten both-pass outcomes, one warning-on-only pass, and no
warning-off-only pass. Across those same pairs, warning-on used 112 file-tool calls
versus 169, including 32 versus 67 searches; trace-reported tokens were 824,364
versus 1,529,667, including cached input. Interrupted pairs are excluded, and
missing usage is unknown. These descriptive results use the earlier frozen
`efbf401` engine and do not establish general success-rate or cost improvements.
[Full outcomes, protocol amendment, traces, and limits](verification.md#paired-model-study-on-the-frozen-engine).

## Historical v0.1.4 live keyword refactor

Hermes with OpenAI `gpt-5.4-mini` renamed a keyword-only parameter in Hermes
Agent's own code. Locus 0.1.4 reported all five incompatible call sites in the
first patch result, before Hermes searched for callers. Hermes confirmed the
locations, repaired the five calls, passed all 26 upstream display tests, and
verified that no drift alerts remained. The prompt staged the helper edit and
gave Hermes no caller locations. [Evidence and scope](verification.md#live-keyword-refactor-on-hermes-agent)

## Check the guard across three repositories

Nineteen matched Hermes patches on pinned source files from Hermes Agent,
HTTPX, and Requests tested breaking and compatible signature edits.
Python's argument binding labeled each selected call. Locus 0.1.4 named
33 of 33 incompatible direct-import call instances, compared with 6 of 33
in v0.1.3. Neither version named a compatible call. Both missed the nine
broken calls in the module-qualified Requests case.
[Reproduce the check](verification.md#signature-changes-across-three-repositories)

## Keyword rename replay

Locus Codegraph 0.1.4 ran a controlled replay of a same-arity keyword rename on
Hermes Agent's own source. Both versions received the same patch. The earlier
version reported zero affected calls. The new version located two gateway calls
and three test calls in the patch result and suggested replacing `max_len=`
with `limit=`. After the scripted repair, all 26 upstream tests passed and the
alert cleared. [Replay commands and results](verification.md#keyword-rename-on-hermes-agent)

## Earlier recorded model session

A real Hermes session with OpenAI `gpt-5.4-mini` changed a shared helper to require
an additional argument. Its actual editing-tool result contained a warning naming
two incompatible callers. The agent repaired both callers, ran all three fixture
tests successfully, and verified zero remaining drift alerts.

- [Complete event transcript](evidence/live-hermes-session.jsonl)
- [Exact prompt](evidence/demo-prompt.txt)
- [Environment, checks, and limitations](verification.md)
- [Real dashboard capture](../assets/locus-live-dashboard.jpg)
- [77-second video](https://github.com/atikur/hermes-locus/releases/download/v0.1.2/locus-submission-demo.mp4)

The earlier 77-second video presents selected actual tool output and the real
dashboard capture, with narration. It is labeled “Recorded Hermes run · edited
for clarity.” The full transcript retains incidental failed commands. Version
0.1.2 changes the installation identity to `locus-codegraph`; the recorded session
used 0.1.1. The video is a controlled capability demonstration, not a measurement
of an unassisted agent's failure rate.

## Reproduce the workflow

Create the synthetic project in an empty destination:

```bash
python3.11 scripts/create_demo_workspace.py ../locus-demo
cd ../locus-demo
python3.11 -m unittest discover -s tests -v
hermes
```

With Locus Codegraph enabled and a model configured, use:

```text
This disposable project is a demonstration of Locus Codegraph. Inspect
@sym:verify_token and @impact:auth/tokens.py. Change verify_token to require
an audience argument. For this demonstration, edit the helper first in a
separate tool call, inspect the returned Locus feedback, then update the affected
callers. Run the relevant tests and report any remaining drift alerts.
```

Separating the helper edit makes the intermediate warning visible. Otherwise,
an agent may correctly update all files in one batch. The standalone
`scripts/run_demo.py` exercises the engine with scripted edits and is useful for
rehearsal; it is not the recorded LLM-driven demonstration.

## Entry links and status

- [Source repository — private during preparation](https://github.com/atikur/hermes-locus)
- [Release CI](https://github.com/atikur/hermes-locus/actions/workflows/tests.yml)
- [Catalog PR search](https://github.com/NousResearch/hermes-agent/pulls?q=is%3Apr+author%3Aatikur+locus-codegraph)
- [Official entry form](https://form.typeform.com/to/FwJLvOln)
- [Organizer announcement](https://x.com/NousResearch/status/2118094256438936062)
- [Catalog admission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)

The repository must remain private until its owner authorizes publication.
Links above do not establish public availability, catalog acceptance, or entry
form completion. The catalog entry must pin the published plugin's exact
40-character commit SHA.
Catalog submission and hackathon registration are separate: the repository and
PR do not establish that the entry form was submitted. If publication is
authorized, the submission checklist includes posting the video on X tagging
`@NousResearch` and supplying the post and catalog PR in the official form.
Catalog listing remains subject to maintainer review.

## Suggested X post

Draft only; the numbers describe the preserved v0.1.4 recording:

> Hermes renamed a Python keyword. Locus Codegraph flagged 5 broken calls in the patch result, before caller search. Hermes fixed them and passed 26 tests. Built for the @NousResearch Plugin Hackathon. AI voiceover. https://github.com/atikur/hermes-locus
