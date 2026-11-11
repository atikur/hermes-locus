# Verification — Locus Codegraph

Verified on 11 November 2026 on macOS (Darwin 25.4.0, arm64).

The local checks below cover **0.1.4** with Python call binding. The
[GitHub workflow](https://github.com/atikur/hermes-locus/actions/workflows/tests.yml)
defines standalone jobs on Linux and macOS, a portable Windows smoke test,
and a fresh plugin install and edit replay on pinned Hermes. The model
evaluation runs separately and requires an API key.

Since version 0.1.2, this project uses the distinct plugin identity
`locus-codegraph` to coexist with an unrelated plugin named Locus. The earlier
fixture session used 0.1.1; its event transcript is preserved.

## Reproducible checks

| Check | Observed result |
| --- | --- |
| Standalone Python 3.11.16, `requirements-dev.txt` | 74 tests passed and 3 Hermes-specific checks skipped |
| Installed Hermes, Python 3.14.7 | 77 tests passed, no skips |
| `hermes plugins validate . --install-deps` | Passed; security scan safe; declarations match registrations |
| `hermes plugins doctor . --ci` | Passed; 4 tools and 3 hooks registered |
| Clean install from local Git with `--ref` and `--enable` | A fresh profile ran `hermes locus impact` and returned the expected callers and test. The installed plugin then emitted a keyword rename warning in a real `patch` result. |
| Hermes payload scanner | Passed |
| `python scripts/run_demo.py` | Scripted engine example completed |
| Web Dashboard | Built the real Hermes UI; verified graph focus, impact, symbol search and signature detail in a browser |
| Desktop | SDK surface admission and JavaScript syntax checked; native desktop interaction has not been manually verified |

Host source used for integration: `NousResearch/hermes-agent` checkout
`01672831e48c1224bc003f834dd9b8c1cfc96a57` (includes the original local catalog
draft; upstream parent `78abaafc`). The plugin does not modify Hermes source.

Run the standalone suite using the README instructions. For the host suite,
use the Hermes interpreter and a disposable `HERMES_HOME`, then run the same
`python -m unittest discover -s tests -v` command. Host-dependent tests are
explicitly skipped when Hermes is unavailable.

## Live keyword refactor on Hermes Agent

Hermes with OpenAI `gpt-5.4-mini` changed a keyword-only parameter in its own
source, from `max_len` to `limit`. The run used a fresh archive of upstream
Hermes commit [`78abaafc`](https://github.com/NousResearch/hermes-agent/commit/78abaafcccec20a546c94e8734a70e38c08a6a69)
and Locus 0.1.4 at commit `c6a07dcf64affe8f90ea9a4498fa3395cca224a8`.

1. The prompt named the helper and requested that Hermes edit it before looking
   at callers. It supplied no caller locations or count.
2. The actual `patch` result warned about all five incompatible call sites
   and suggested replacing `max_len=` with `limit=` (transcript line 5).
3. Hermes searched to confirm the list (line 6), then repaired all five calls.
4. Hermes ran the upstream display suite: **26 passed** (line 25).
   `locus_hotspots` then reported **zero open signature drift alerts** (line 31).

An independent comparison checked 24,306 archived files and symlinks against
upstream. Only the four expected files changed, with the requested helper
edit and five keyword repairs. This was one staged session to show when the
warning arrives. The [scripted comparison](#keyword-rename-on-hermes-agent)
tests the same edit on two versions of Locus.

The 60-second demo opens with the recorded run. Its final nine seconds show
the Hermes Web Dashboard replaying the same edits and the alert clearing.
All four repaired files in the replay match the live run byte for byte.

[60-second demo](https://github.com/atikur/hermes-locus/releases/download/v0.1.4/locus-live-keyword-refactor.mp4) ·
[Full event transcript](evidence/live-keyword-session.jsonl) ·
[Prompt](evidence/live-keyword-prompt.txt) ·
[Independent verification](evidence/live-keyword-verification.json)

The prompt and transcript redact only the local workspace path. Both the live
run and scripted comparison used the same source archive, test dependencies,
and scan cap and timeout settings described below.

## Keyword rename on Hermes Agent

Both runs started from fresh archives of Hermes Agent commit
[`78abaafc`](https://github.com/NousResearch/hermes-agent/commit/78abaafcccec20a546c94e8734a70e38c08a6a69)
and indexed 17,025 files. The integration test sent the same edit and repair
payloads through Hermes's real `patch` tool. The edit renamed the required
keyword `max_len` to `limit` in `agent/display.py`'s `prepare_tool_preview` and
updated the function body. The parameter count and kinds stayed the same.

| Observation | v0.1.3 | v0.1.4 |
| --- | ---: | ---: |
| Original display tests before the edit | 26 passed | 26 passed |
| Call sites named in the signature edit result | 0 | 5 |
| Original display tests after the edit | 23 passed, 3 failed | 23 passed, 3 failed |
| Original display tests after updating all five calls | 26 passed | 26 passed |
| Open drift alerts after repair | 0 | 0 |

The five diagnostics point to two gateway calls and three test calls. Each
explains that `max_len` is unexpected and suggests reviewing a replacement
with `limit`. The test failures reproduced Python's `TypeError` for the old
keyword. The browser dashboard also displayed caller paths, errors, and
suggestions on a small fixture, and cleared the alert after a repair and rescan.

[v0.1.3 replay](evidence/keyword-refactor-baseline.json) ·
[v0.1.4 replay](evidence/keyword-refactor-candidate.json) ·
[58-second comparison](https://github.com/atikur/hermes-locus/releases/download/v0.1.4/locus-keyword-replay.mp4) ·
[Integration test](../tests/integration/verify_hermes_refactor.py)

The replay scripts the edits and makes zero model calls. It measures the alert
and its clearance on one real refactor, with the same edits in both runs. The
scratch profiles use a 20,000-file cap and a 120-second hook timeout to index the
whole selected checkout. The test runs the upstream display file with
`--noconftest` because the global process cleanup hook needs process enumeration
that is unavailable in the sandbox.

To reproduce, use the Hermes Python interpreter and install `pytest==9.1.1`
and `pytest-asyncio==1.4.0` into an isolated directory. Point `--source` at a
Hermes checkout containing the pinned commit and `--runtime` at the installed
Hermes runtime. Use `--variant baseline` with a separate output file to replay
the v0.1.3 commit from this plugin's history.

```bash
/path/to/hermes/python tests/integration/verify_hermes_refactor.py \
  --source /path/to/hermes-agent --runtime /path/to/hermes-runtime \
  --pytest-deps /path/to/test-deps --variant candidate --output candidate.json
```

## Signature changes across three repositories

We ran 19 matched edits through the real Hermes `patch` tool on selected
original Python files from pinned revisions of Hermes Agent, HTTPX, and
Requests. Each edit starts from the original files. Edits include renamed
keywords, required keywords, positional-only changes, compatible additions,
and body-only controls. CPython's `inspect.Signature.bind` independently
labels which original calls still bind after each edit.

| Call instances in the selected edits | v0.1.3 | v0.1.4 |
| --- | ---: | ---: |
| Incompatible direct-import calls named | 6 / 33 | **33 / 33** |
| Compatible direct-import calls named | 0 / 33 | **0 / 33** |
| Incompatible module-qualified calls named | 0 / 9 | **0 / 9** |
| All incompatible calls named | 6 / 42 | **33 / 42** |

The same source call can appear in several edits. These counts measure call
binding for the selected edits, including one module-qualified Requests case
that Locus does not resolve precisely. They do not estimate accuracy across
entire repositories or verify runtime behavior. No model calls are involved.

[Pinned source revisions, oracle labels and actual patch results](evidence/signature-corpus.json) ·
[Replay script](../tests/integration/verify_signature_corpus.py)

Run with the Hermes interpreter. The script checks each upstream source blob
against its Git hash. External Python files are fetched from GitHub unless
`--cache` points to previously downloaded copies.

```bash
/path/to/hermes/python tests/integration/verify_signature_corpus.py \
  --source /path/to/hermes-agent --runtime /path/to/hermes-runtime \
  --candidate-ref HEAD --check-supported --output signature-corpus.json
```

## Fewer SQLite writes during an edit

Locus recalculates resolution and ranking after an edit. The final v0.1.4
engine skips database updates when the resolved paths and graph statistics
have stayed the same.

We compared the previous and optimized engines using identical copies of a
17,025-file Hermes Agent index on one Apple Silicon Mac. Two repeats alternated
execution order. The measured relink and commit step after reparsing
`agent/display.py` took a median of **7.35 s before and 4.39 s after**. The
number of rows updated in that step dropped from **1,219,296 to 311**. The
benchmarks for a no-change relink and a new dependency also produced identical
graph digests before and after the optimization.

This measures the database relink step with an existing index. It excludes
startup scans, source parsing, and model calls. Three simultaneous trial
sessions on this Mac hit Hermes's hook timeout while using the raised
20,000-file cap. We switched the model study to serial runs.

[Raw timings and graph digests](evidence/relink-benchmark.json) ·
[Benchmark runner](../tests/integration/benchmark_relink.py)

## Matched Hermes model trials

We ran GPT-5.4 mini on three keyword renames in fresh copies of the same
pinned Hermes source. Each task ran twice with the same prompt and file tools.
Both arms loaded Locus and computed drift. The `drift_guard` setting only
controlled whether Hermes saw the warning in the edit result.

The prompt asked for the helper edit first and provided no caller locations.
An evaluator ran the upstream tests after each session and checked the final
source against the requested rename, including that all test assertions stayed
intact. The model did not run tests during these sessions. This measures what
the model returned after a staged edit without a test loop. Run order was
warning on, then warning off.

One initial accounting label missed a real caller in the image generation
plugin. The warning identified it. An independent check of the pinned source
and actual edit diffs added it to the caller list for both arms and retained
the original results alongside that correction.

| Rename | First warning named | Warning on | Warning off |
| --- | ---: | --- | --- |
| Display preview | 5 / 5 | 5 calls repaired, 26 tests passed | 3 of 5 calls repaired, 23 passed and 3 failed |
| Replay history | 5 / 5 | 5 calls repaired, 6 tests passed | 5 calls repaired, 6 tests passed |
| Auxiliary accounting | 2 / 3 | 3 calls repaired, 11 tests passed | 3 calls repaired, 11 tests passed |

All **12 named locations** were correct and arrived before the model looked
outside the helper. The missing accounting call uses a repeated local import
that the resolver treats as ambiguous. Hermes found it during its search.
The display warning-off run also rewrote unrelated helper logic in its initial
patch, before a warning could have appeared. These three pairs show observed
outcomes, with variation in the model's first edit and no estimate of general
repair success.

We also supplied the exact display patch in a separate pair. The warning-on
model applied only the signature line, repaired five callers, and left the
helper body broken. The warning-off model applied the intended helper edit
but left two callers stale. Both runs failed tests. The tool traces retain
this deviation from the supplied patch.

For the final control, we dispatched the identical display edit through the
real Hermes patch tool before each model session. Both tool results matched
after local path normalization and removal of the warning field. Each repair
prompt included its actual patch result. With the warning enabled, Hermes
received five exact locations before its first lookup. With the warning
disabled, it received none. Both arms repaired all five callers and passed
all 26 tests. The control verifies the difference in feedback, with the same
repair outcome in this pair.

[Six staged runs and independent diff audit](evidence/refactor-pairs/staged) ·
[Supplied-patch pair](evidence/refactor-pairs/supplied-patch) ·
[Identical pre-applied patch and control](evidence/refactor-pairs/replayed-helper) ·
[Evaluation runner](../tests/integration/evaluate_refactor_feedback.py)

With `OPENAI_API_KEY` configured, the following command repeats the six staged
sessions. Each gets a fresh profile and workspace and makes its own model
API requests.

```bash
/path/to/hermes/python tests/integration/evaluate_refactor_feedback.py \
  --source /path/to/hermes-agent --runtime /path/to/hermes-runtime \
  --pytest-deps /path/to/test-deps --scratch /path/to/scratch \
  --output paired-results
```

Add `--task display --replay-helper-edit` to repeat the final two-session
control with a pre-applied edit.

## Real model-assisted refactor

A real Hermes session using `gpt-5.4-mini` completed the controlled fixture
workflow in approximately 32 seconds:

1. Hermes changed `verify_token(token)` to `verify_token(token, audience)` using
   its actual `patch` tool.
2. That tool result contained a Locus alert naming `services/checkout.py:4` and
   `services/receipts.py:4`, both still passing one argument.
3. Hermes patched both callers to pass `"checkout"`.
4. Hermes ran the fixture's three unit tests successfully.
5. `locus_hotspots` reported zero open signature drift alerts.

The [complete event transcript](evidence/live-hermes-session.jsonl) and
[prompt](evidence/demo-prompt.txt) are included. Only the local workspace path
prefix has been replaced. The transcript retains incidental unsuccessful
commands as well as the successful refactor. The prompt deliberately requested
a separate helper edit so the intermediate warning would be visible; this is a
capability demonstration, not an agent failure-rate benchmark.

The recorded run also highlighted a mismatch between the test-reachability
heuristics in impact and hotspot reports. Version 0.1.1 now uses the same
three-hop default for both, with a regression test for the boundary.

A separate deterministic integration run used real Hermes plugin discovery,
lifecycle dispatch and actual file tools, with no mocked execution. It verified
that a warning persists through a body-only edit and clears after a multi-file
caller repair. The unit suite independently covers that lifecycle regression.

## Version 0.1.3 reliability checks

- Incomplete Python edits keep the last valid signatures and call graph. Both
  scan responses and editing-tool results report the parse failure separately.
  A fresh real-Hermes run verified invalid syntax → indexing warning → syntax
  repair → caller repair → passing tests, with no model call or mocked execution.
- Editing hooks obey the same ignore, hidden-directory, generated-directory,
  and symlink rules as full scans. Deletion still removes old indexed entries.
- Scans explicitly report when the configured file cap truncates the workspace.
- Cycle detection handles 2,500-node chains and cycles without Python recursion.
  An independent reachability oracle matched 9,096 small exhaustive/random graphs.
- Invalid alert mutations return client errors; workspace-scoped dismissal does
  not resolve another workspace's alert. Browser warnings, mutation failures,
  and stale workspace responses are handled explicitly.
- Browser verification confirmed that a failed parse preserves all five fixture
  files and nine symbols, shows the [syntax warning](evidence/syntax-warning.jpg),
  and clears it after syntax repair and a successful rescan.

See the [larger-workspace trial](performance.md) for measured performance and
the reproducible benchmark command. Native Desktop interaction remains an
explicit verification gap; its admission, syntax, and shared API checks pass.

## Raw check output

- [Host tests](evidence/tests-host.txt)
- [Standalone tests](evidence/tests-standalone.txt)
- [Plugin validation](evidence/plugin-validation.txt)
- [Plugin doctor](evidence/plugin-doctor.txt)

These checks establish the tested behavior and environment, not universal
language accuracy or a performance guarantee. Static-analysis limitations are
listed in the README.
