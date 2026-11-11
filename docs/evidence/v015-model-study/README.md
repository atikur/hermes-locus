# Preregistered warning-on/off study

The original schedule contains **24 attempts** on four selected public Hermes refactors, using GPT-5.4-mini with low reasoning. **23 sessions completed; one was deliberately interrupted for a privacy-boundary amendment.** No replacement session was added. There are **11 complete matched pairs**, not 12.

The measured plugin engine is `efbf401befb73fe11e1a9c5985ef3783935090db8a38ebe6c26f7c06611553ac`. The later final candidate `c622b20c86fdd87d9146f784f03e652ca7958abb601a65db582b4caf52eafc5c` adds integrity fixes and has separate deterministic verification; it was not rerun through this model study.

## Complete matched pairs

| Measurement | Warning on | Warning off |
| --- | ---: | ---: |
| Successful refactors | 11 / 11 | 10 / 11 |
| File-tool calls | 112 | 169 |
| Searches | 32 | 67 |
| Reads | 35 | 56 |
| Edits | 45 | 46 |
| Reported tokens, including cached input | 824,364 | 1,529,667 |
| Summed model duration | 637.011 s | 718.755 s |

Ten pairs passed in both conditions; one passed only with warnings; none passed only without warnings. Warning-on used fewer tools and reported tokens in all 11 pairs. Searches and reads were lower in 10 pairs and equal in one. Model duration was lower in eight pairs and higher in three. These are descriptive results from four purposively selected tasks, not a representative benchmark or a general success-rate claim.

Across all completed sessions, warning-on passed 12/12 and warning-off 10/11. The extra warning-on accounting repeat 3 is unpaired and excluded from the table. The known usage subtotal is **2,385,584 reported tokens across 23 accounted sessions**, including 1,957,376 cached-input tokens. The interrupted attempt has no final usage accounting. No dollar fields were emitted, so no dollar estimate is reported.

## Outcome checks and preserved failures

Each pair received the same pre-applied helper patch. Both arms loaded Locus and computed drift; only warning delivery differed. Pair order was randomized before any model calls. The model had file tools only, with a separate evaluator running the upstream tests after the session.

A pass required a successful model/CLI exit, every expected caller repair, independent Python signature binding, preserved whole-file ASTs for the helper/callers including test assertions, no unexpected file changes, and passing upstream tests. The complete checkout fingerprint audit covered 24,306 files. The untouched, helper-only broken, and reference-repaired test preflights are retained in [results/preflight](results/preflight).

The genuine failure, [accounting repeat 1 warning-off](results/accounting-1-warning-off/result.json), changed an unrelated `_validate_llm_response(provider=...)` keyword to `provider_id`, causing a `TypeError`. Its full trace, diff, source audit and failing test log are preserved. The privacy interruption, [accounting repeat 3 warning-off](results/accounting-3-warning-off/result.json), is a separate evaluator intervention and does not count as comparative task failure. Raw `summary.json` keeps the attempt-level failure flag; [analysis-final.json](analysis-final.json) applies the explicit interruption classification.

## Privacy amendment and audit

The evaluator boundary confines file-tool paths to each disposable public checkout. Review found that Hermes can split nonexistent search paths into multiple roots and accepts additional patch-header forms. The amended boundary validates all fallback roots and rejects unrecognized patch headers. Legitimate public multi-root searches still work. The original and amended guard source are retained in [scoring](scoring).

The active tenth attempt was interrupted, its partial trace retained, and only the remaining original schedule resumed. The amended guard passed eight actual Hermes dispatch checks. All prior requests were checked before resumption. The final [request audit](all-session-request-boundary-audit.json) checks all **24 traces and 287 model tool requests** against a reconstructed public baseline whose fingerprint matches preregistration: no outside-root requests, boundary denials or unexpected source files were found. This is a saved-request/file-path audit, not an operating-system sandbox security proof.

## Review and reproduction

- [Protocol and original schedule](results/protocol.json)
- [Privacy-only protocol amendment](protocol-amendment-1.json)
- [Per-session results, paired resource totals and limits](analysis-final.json)
- [Frozen plugin file manifest](final-freeze-manifest.json)
- [Captured-artifact and packaged-file checksums](checksum-manifest.json)

All 24 attempt traces, initial helper results, prompts, complete diffs, source audits, test logs and stderr captures are included. Traces were already sanitized when captured; no pre-redaction raw trace or checksum was retained. The checksum manifest identifies each existing source artifact and its packaged copy, with every additional path redaction recorded. No raw Hermes checkout or frozen private plugin source tree is copied into this package.

The reporting script can reproduce the descriptive analysis without model calls:

```sh
python tests/integration/summarize_refactor_feedback.py \
  --results docs/evidence/v015-model-study/results \
  --amendment docs/evidence/v015-model-study/protocol-amendment-1.json \
  --output /tmp/locus-study-analysis.json
```
