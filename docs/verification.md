# Verification — Locus Codegraph

Verified on 11 November 2026 on macOS (Darwin 25.4.0, arm64).

The current private candidate is **0.1.5**. Its status is separated below from
the preserved **0.1.4** evidence. The
[GitHub workflow](https://github.com/atikur/hermes-locus/actions/workflows/tests.yml)
defines standalone jobs on Linux and macOS, a portable Windows smoke test,
and a fresh plugin install and edit replay on pinned Hermes. The model
evaluation runs separately and requires an API key.

Since version 0.1.2, this project uses the distinct plugin identity
`locus-codegraph` to coexist with an unrelated plugin named Locus. The earlier
fixture session used 0.1.1; its event transcript is preserved.

## Version 0.1.5 candidate

This candidate extends supported Python resolution to module-qualified calls,
import aliases, relative imports, static re-exports, and independently scoped
function-local imports. Ambiguous symbol queries return exact
`path::qualified_name` candidates. Dynamic bindings and same-file drift remain
outside the precise cross-file guard; no-alert results are not a proof of safety.

Both interfaces now place caller evidence first: locations, binding errors,
available repair suggestions, and draft repair context. Stored alerts retain
all detected caller locations. Tool-result text remains bounded and explicitly
points to the complete stored alert when it omits locations. Persistent scan
coverage, parse failures, and previously indexed files that are now skipped
accompany reports and the UI. Stale sources retain established caller evidence as
**Needs verification** until current source can be checked; the UI shows up to
20 stale paths with the full count disclosed. A dismissal is an
acknowledgment, and closed history does not imply passing tests.

| Candidate check | Status |
| --- | --- |
| Hermes catalog admission | [`plugins validate --install-deps` passed](evidence/v015-admission-release.json), with zero warnings, safe security scan, matching registrations, and accepted Desktop SDK surface; [stderr is empty](evidence/v015-admission-release-stderr.txt). This local check does not establish catalog eligibility or acceptance |
| Fresh exact-commit local Git install and replay | [Passed at `e71824bc` / `c622b20c`](evidence/v015-install-replay.json): 17,025 files indexed; edit named five callers; broken state had 3 failing/23 passing tests; repair restored 26 passing tests and zero alerts |
| Hermes runtime doctor | [`plugins doctor --ci` passed](evidence/v015-doctor-release.txt), with four tools and three hooks |
| Dashboard API regressions | [10 local checks passed](evidence/v015-tests-stale-api.txt), including exact totals, workspace-scoped mutations, persistent parse/cap/stale warnings, retained scan caps, and stale → rechecked-but-broken → repaired alert transitions |
| Shipped Web/Desktop component contracts | [10 local Node checks passed](evidence/v015-tests-stale-ui.txt): all stored caller rows, repair context, exact symbol selection, incomplete/error states, explicit uncertainty, bounded stale-path rendering with total disclosure, keyboard navigation, and profile/connection cache isolation |
| JavaScript syntax | Both shipped bundles pass `node --check` |
| Complete Python suite in Hermes, Python 3.14.7 | [123 checks passed, no skips](evidence/v015-tests-release-host.txt); the earlier [109-check](evidence/v015-tests-postfreeze-host.txt) and [106-check](evidence/v015-tests-host.txt) runs are preserved. Logs retain environment/deprecation/resource diagnostics |
| Minimum supported Python 3.10.22, standalone with pinned development requirements | [120 passed and 3 Hermes-runtime-only checks skipped](evidence/v015-tests-release-python310.txt), out of 123 discovered tests; the [earlier 106-pass/3-skip record](evidence/v015-tests-python310.txt) is preserved. Python 3.10 is included in the Ubuntu CI matrix |
| Evaluation-protocol validity | [7 checks passed](evidence/v015-tests-evaluator-complete.txt), including paired input control, source-integrity oracle, multi-path file boundary, stopping rules, and excluding interrupted pairs from comparisons |
| Pinned source and resolution corpora | Final reruns complete at `e71824bc` / `c622b20c`; all counts below reproduced, including the two same-file misses |
| Class-bound call extension | Measured separately on the extended engine: [constructors and static/class methods](#class-bound-calls-on-the-extended-engine). The other rows in this table were not repeated on it |
| Matched model study | [24 attempts preserved: 23 completed and one privacy interruption](evidence/v015-model-study/analysis-final.json). Eleven complete pairs: 10 both pass, one warning-on-only pass, none warning-off-only |
| Actual Web and native Desktop interaction | Passed the [synthetic-fixture interaction checklist](evidence/v015-native-verification.md), including eight callers, complete unsent repair draft, keyboard navigation, live parse warnings, repair, and dismissal; [structured record](evidence/v015-native-verification.json) |
| Actual stale-source Web/Desktop follow-up on the final engine | Passed the [oversized-caller workflow](evidence/v015-native-stale-verification.md): six stale caller records retained alongside two confirmed failures, persistent warnings after Web reload, recheck without false clearance, and cleanup with three passing fixture tests |

The exact-commit install replay used a fresh profile, local Git source, a
20,000-file cap, and a 120-second hook timeout. It made zero model calls. The
[first attempt](evidence/v015-install-environment-failure.json) stopped during
Hermes's isolated-runtime setup because sandbox DNS blocked downloading its
`packaging` dependency. Retrying with network access used unchanged source and
passed all eight replay steps; [installer/replay log](evidence/v015-install-replay.txt).
This establishes local installation and integration, not public catalog admission.
The separate [final performance trial](performance.md#final-v015-exact-commit-observation)
kept the default 30-second edit-hook timeout.

Native setup exposed a host limitation in the packaged Desktop 0.22 build:
a plugin installed and enabled only in a selected named profile rendered its
pane, but the shared backend launched in `default` returned `Plugin not found`.
The bundled server discovers API plugins and applies its middleware enablement
gate using the process launch profile. Installing and enabling the same plugin
for that launch profile made the API available. The subsequent native flow
passed in that setup. Upstream host source was not modified.

The final candidate engine is
`c622b20c86fdd87d9146f784f03e652ca7958abb601a65db582b4caf52eafc5c`.
After the model-study freeze, review found that an already broken caller could
lose static confidence after a conditional import or dynamic scope change and
be incorrectly cleared. The candidate now keeps that established risk as
**Needs verification** until a supported repair or removal. [Four explicit
regression fixtures](evidence/v015-postfreeze-integrity.json) retain the alert
through confidence loss and a forced scan, then clear it after supported repair.
That earlier fix was covered by the preserved 109-check host run. Further
review found that an existing source becoming oversized, excluded, or otherwise
unavailable could be mistaken for a removed caller. The candidate now retains
its last-valid index, reports persistent stale coverage, and prevents stale
caller or target data from proving a repair. Source excerpts are omitted for
stale or unparseable paths and bounded to the source-size limit. The current
123-check host suite includes these [skipped-source regressions](../tests/test_skipped_sources.py)
and the API transition check. This is separate evidence from the frozen model
study; its engine and observations have not been replaced.

The deterministic corpora below executed through real Hermes editing hooks with
zero model calls using exact Git checkpoint
`e71824bc12d6cde8951d95c8545ad6eda4ca1263`, engine
`c622b20c86fdd87d9146f784f03e652ca7958abb601a65db582b4caf52eafc5c`.
The earlier frozen-engine [signature](evidence/v015-signature-corpus.json) and
[resolution](evidence/v015-resolution-corpus.json) results remain unchanged.
[Artifact checksums and sanitization record](evidence/v015-evidence-manifest.json).

| Selected corpus | Cases | Incompatible call instances named | Compatible call instances named |
| --- | ---: | ---: | ---: |
| [Pinned Hermes / HTTPX / Requests signature edits](evidence/v015-signature-corpus-release.json) | 19 | 42 / 42 | 0 / 34 |
| [Additional pinned real-source resolution edits](evidence/v015-resolution-corpus-release.json) | 12 | 22 / 24 | 0 / 28 |
| [Separately authored executable resolution controls](evidence/v015-resolution-corpus-release.json) | 25 | 16 / 16 | 0 / 13 |

The two missed instances are the same source location,
`agent/retry_utils.py:167`, under two different mutations. That call is in the
edited file, outside the external-caller guard. All 22 cross-file incompatible
instances in that corpus were named. Counts repeat source calls across edits;
they are not counts of independent repositories or agent tasks. The 19-case
rerun includes one compatible module-qualified call in its all-case denominator;
the preserved direct-import comparison below has 33 compatible instances.

The first final-commit resolution attempt stopped during archive setup before
any candidate case executed. Its [declaration](evidence/v015-resolution-corpus-release-setup.json)
and [error record](evidence/v015-resolution-corpus-release-setup-error.txt)
remain separate. Creating the missing installation parent directory fixed the
harness; the rerun used identical declared cases, labels, and candidate engine.

The [resolution corpus](../tests/integration/verify_resolution_corpus.py) uses
pinned source projections and separately authored executable controls. It keeps
same-file misses in the overall denominator. These targeted cases were selected
during development; they are not a representative or blind accuracy benchmark.

### Class-bound calls on the extended engine

A later local extension checks two further kinds of supported cross-file call:
calling an imported class, which binds to its constructor, and
`Class.method()` for static and class methods. It applies to bare top-level
classes with no decorator, metaclass, or `__new__`, whose bases are absent,
`object`, `ABC`, built-in exceptions, or such classes in the same module.
Instance-method calls, decorated classes including dataclasses, and classes
with an imported base remain unchecked.

This engine is
`fb368374ff8975569b5dcb83aa1aaca2c92d0c458aa3664f61ef8f02a8250363`, measured
as working-tree changes on top of commit
`d72752365b3256ea5bd3e02a1d4ec2306f7aeedb` before they were committed. The
evidence identifies it by this digest, not by an installed commit. The
install replay, catalog admission, runtime doctor, performance trial, and
Web/Desktop checks above were not repeated on it; hosted CI has not run; and
the model study did not use it.

| Check on the extended engine | Result |
| --- | --- |
| Complete Python suite in Hermes, Python 3.14.7 | [130 checks passed, no skips](evidence/v015-class-calls-tests-host.txt), including seven [class-bound call checks](../tests/test_class_bound_calls.py) |
| Minimum supported Python 3.10.22, standalone | [127 passed and 3 Hermes-runtime-only checks skipped](evidence/v015-class-calls-tests-python310.txt), out of 130 discovered tests |
| Original 19-case signature corpus | [42 / 42 incompatible and 0 / 34 compatible instances named](evidence/v015-class-calls-signature-corpus.json), at the same sites as the recorded run |
| Original 37 resolution cases | [22 / 24 and 16 / 16 incompatible, 0 / 41 compatible](evidence/v015-class-calls-resolution-corpus.json), at the same sites as the recorded run, including the two same-file misses |

Protocol 2 of the [resolution corpus](../tests/integration/verify_resolution_corpus.py)
adds 49 class-bound cases in two new groups. The original 37 cases keep
identical labels and patches. Every case ran through real Hermes editing hooks
with zero model calls, on the [extended engine](evidence/v015-class-calls-resolution-corpus.json)
and, for contrast, on the [previous engine](evidence/v015-class-calls-resolution-corpus-previous-engine.json)
`c622b20c`.

| Class-bound group | Cases | Incompatible named, extended engine | Incompatible named, previous engine | Compatible named, either engine |
| --- | ---: | ---: | ---: | ---: |
| Pinned Hermes classes | 16 | 25 / 26 | 0 / 26 | 0 / 38 |
| Separately authored executable class fixtures | 33 | 16 / 20 | 0 / 20 | 0 / 13 |

The pinned group applies four mutations each to two constructors
(`DebugSession`, `SignalRateLimitError`), one static method
(`LlamaServerSupervisor._terminate_tree`), and one class method
(`TeamsPipelineStore.build_notification_receipt_key`). Labels come from
`inspect.Signature.bind` on the member's declared parameters without the
implicit receiver. Its one miss is `hermes_cli/local_runtime/supervisor.py:301`,
a call in the edited file.

The authored fixtures are labelled only by executing them before and after the
edit. All 16 supported breaks were named: direct, aliased, module-qualified,
re-exported, and function-local constructor calls; inherited and default
constructors; an exception subclass; and static and class methods, including
inherited ones. None of 13 calls that stay valid was named: a positional call
unaffected by a rename, other classes and modules with the same names,
shadowed bindings, and classes whose decorator, metaclass, `__new__`, or
patched `__init__` keeps the edited signature from binding. The four retained
misses are real breaks outside the supported set: two calls through an
instance, a dataclass constructor, and a class with an imported base.

These cases were written alongside the extension, and the pinned targets were
chosen among classes the extended engine already resolves. They show behavior
where the check applies, not how often it applies, and are not a blind or
representative benchmark. Labels were prepared before Locus ran, and the run's
case manifest equals that plan; the plan embeds full pinned sources, so only
its checksum is in the [evidence manifest](evidence/v015-evidence-manifest.json).

### Paired model study on the frozen engine

The completed study used `gpt-5.4-mini` on four selected Hermes refactor tasks,
with three repeats and two warning conditions: 24 planned attempts. It preserved
**23 completed sessions and one deliberately interrupted control attempt**.
The primary comparison uses only **11 complete pairs**: ten passed in both arms,
one passed only with the warning, and none passed only without it. This is a
small repeated-task study, not evidence of a general success-rate improvement.
[All outcomes and paired measurements](evidence/v015-model-study/analysis-final.json).

| Measure, across the same 11 complete pairs | Warning on | Warning off |
| --- | ---: | ---: |
| Repairs passing the independent checks | 11 / 11 | 10 / 11 |
| Model file-tool calls | 112 | 169 |
| Search calls | 32 | 67 |
| Read calls | 35 | 56 |
| Edit calls | 45 | 46 |
| Trace-reported total tokens, including cached input | 824,364 | 1,529,667 |

Warning-on used fewer tool calls and reported tokens in all 11 pairs, fewer
search/read calls in ten pairs with one tie, and fewer edits in one pair with
ten ties. Trace-reported model duration was lower in eight pairs and higher in
three. These are observations on the selected tasks, not savings or latency
guarantees. Dollar costs were absent from the traces and are not estimated.

For completeness, all twelve completed warning-on sessions passed; ten of the
eleven completed warning-off sessions passed. The extra warning-on session is
excluded from the matched comparison. The known 23-session token subtotal is
2,385,584; the interrupted attempt's final usage is unknown, not zero.

The [evaluator](../tests/integration/evaluate_refactor_feedback.py) freezes inputs,
records the protocol before model calls, randomizes paired order with a fixed
seed, and uses the same pre-applied helper edit in each arm. Independent checks
require the intended repairs, retained assertions, no unexpected file edits,
and passing upstream tests. The model had file tools only; the evaluator ran
tests separately. Both arms load and compute Locus drift; only warning
delivery differs. This does not measure overhead against running without Locus.

The study retains engine
`efbf401befb73fe11e1a9c5985ef3783935090db8a38ebe6c26f7c06611553ac`;
its results are not reruns of the final `c622b20c` candidate. A documented
[privacy amendment](evidence/v015-model-study/protocol-amendment-1.json) interrupted
one control before any tool calls to harden multi-path search and lenient patch
parsing boundaries. No replacement was run; that incomplete pair is excluded
rather than counted as a control failure. The
[all-session request audit](evidence/v015-model-study/all-session-request-boundary-audit.json)
records 287 tool requests and no outside-workspace requests or boundary denials.
The amendment changed the evaluator's file boundary, not the candidate engine,
tasks, or scoring. Protocol, traces, checks, and redaction checksums are retained
in the [study evidence](evidence/v015-model-study/checksum-manifest.json).

To prepare a new study without model calls, use new empty output and scratch
directories:

```bash
/path/to/hermes/python tests/integration/evaluate_refactor_feedback.py \
  --source /path/to/hermes-agent --runtime /path/to/hermes-runtime \
  --pytest-deps /path/to/test-deps --scratch /path/to/scratch \
  --plugin-ref WORKTREE --repeats 3 --seed 20261111 \
  --output candidate-model-study
```

`--run-models` explicitly enables API requests with a configured key. Scheduled
sessions are not completed evidence; retain interruptions and stopping reasons.

## Historical v0.1.4 reproducible checks

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
| Desktop | SDK surface admission and JavaScript syntax checked; native desktop interaction was not manually verified for this historical release |

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

The local historical 60-second demo opens with the recorded run. Its final nine seconds show
the Hermes Web Dashboard replaying the same edits and the alert clearing.
All four repaired files in the replay match the live run byte for byte.

The historical video was not published as a release asset; the saved evidence is linked here.

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
[Integration test](../tests/integration/verify_hermes_refactor.py)

A local historical 58-second comparison video was prepared but not published; the replay artifacts above remain available. The replay scripts the edits and makes zero model calls. It measures the alert
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
that v0.1.4 did not resolve precisely. They do not estimate accuracy across
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
that the v0.1.4 resolver treated as ambiguous. Hermes found it during its search.
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

The current evaluator has evolved since these historical sessions. To run the
legacy staged mode with the current candidate, explicitly select the three
historical tasks; this is a new run, not a replay of the original model outputs.
With `OPENAI_API_KEY` configured, `--run-models` makes model API requests:

```bash
/path/to/hermes/python tests/integration/evaluate_refactor_feedback.py \
  --source /path/to/hermes-agent --runtime /path/to/hermes-runtime \
  --pytest-deps /path/to/test-deps --scratch /path/to/scratch \
  --task display --task replay --task accounting --staged-edit \
  --plugin-ref WORKTREE --run-models --output paired-results
```

Use only `--task display` and omit `--staged-edit` for a new two-session
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
the reproducible benchmark command. Native Desktop interaction was an explicit
gap in these earlier checks; the v0.1.5 interaction record above closes that gap
for its selected synthetic workflow.

## Raw check output

- [Host tests](evidence/tests-host.txt)
- [Standalone tests](evidence/tests-standalone.txt)
- [Plugin validation](evidence/plugin-validation.txt)
- [Plugin doctor](evidence/plugin-doctor.txt)

These checks establish the tested behavior and environment, not universal
language accuracy or a performance guarantee. Static-analysis limitations are
listed in the README.
