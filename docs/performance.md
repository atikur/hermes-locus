# Larger-workspace trial

## Final v0.1.5 exact-commit observation

On 11 November 2026, Python 3.14.7 on macOS 26.4 arm64 measured a fresh
Hermes archive at `78abaafcccec20a546c94e8734a70e38c08a6a69`, using the exact
plugin Git archive `e71824bc12d6cde8951d95c8545ad6eda4ca1263` and engine
`c622b20c86fdd87d9146f784f03e652ca7958abb601a65db582b4caf52eafc5c`.
With a raised 20,000-file cap, the scan indexed **17,025 files, 192,406 symbols,
and 66,779 edges**, without parse errors or file-cap truncation.

| Operation | Observed wall time |
| --- | ---: |
| Scan with a fresh database | 65.50 s |
| Cached scan, no source changes | 1.98 s |
| Real Hermes one-file keyword-rename patch | 6.76 s |
| Real Hermes three-file caller-repair patch | 6.51 s |

Each operation ran once with operating-system caches unchanged. Scan timings
call `index_workspace` directly; edit timings include Hermes editing, lint,
hook indexing, and result transformation. They do not isolate plugin overhead.
The rename returned five affected sites and one open alert; the repair left
zero open alerts. No model calls were made. The edit-hook callback kept its
30-second default; the 65.50-second cold scan ran directly, not inside a startup
hook. The cap exceeded the default 2,500 files.
[Exact measurements, settings, and revisions](evidence/v015-performance-release.json).

This is one workspace observation, not a latency distribution, performance
guarantee, or measured general speedup over the earlier observations below.

## Earlier v0.1.5 development observation

On 11 November 2026, Python 3.14.7 on macOS 26.4 arm64 indexed a fresh archive
of Hermes commit `78abaafcccec20a546c94e8734a70e38c08a6a69` with a raised
20,000-file cap. It indexed **17,025 files, 192,406 symbols, and 66,779 edges**,
with no parse errors or file-cap truncation.

| Operation | Observed wall time |
| --- | ---: |
| Scan with a fresh database | 73.65 s |
| Cached scan, no source changes | 2.72 s |
| Real Hermes one-file keyword-rename patch | 9.42 s |
| Real Hermes three-file caller-repair patch | 5.88 s |

Each operation ran once; operating-system caches were unchanged. Scan timings
call `index_workspace` directly. Patch timings include Hermes file editing,
linting, hook indexing, and result transformation, so they do not isolate plugin
overhead. The rename returned five affected sites and one open alert; the repair
left zero open alerts. No model calls were made. The 30-second edit-hook callback
timeout was unchanged; the 73.65-second cold scan was measured directly, not
inside a startup hook.

The measured engine SHA-256 was
`f37ddb1fbc088b7f55df42acba3df6032fa0c105d10078ecfe7d00906b926226`.
This precedes the report-count correction in the frozen corpus/model engine
`efbf401befb73fe11e1a9c5985ef3783935090db8a38ebe6c26f7c06611553ac`.
These earlier timings belong only to that recorded snapshot. The final candidate adds caller-confidence preservation, stale-source handling,
and bounded source excerpts; its separate exact-commit observation appears
above. These runs are not a controlled speedup comparison.
[Earlier raw measurements and settings](evidence/v015-performance.json).

This is one observation on one workspace, not a latency distribution or a
performance guarantee. The full scan and larger file cap differ from the older
subset trial below, so the two tables are not a matched speed comparison.

## Historical v0.1.3 bounded-subset observation

Measured on 11 November 2026 using an Apple M4 with 16 GiB RAM, macOS 26.4 arm64,
and Python 3.11.16. The workspace was a Hermes Agent source checkout at
`01672831e48c1224bc003f834dd9b8c1cfc96a57`. Locus Codegraph's 0.1.3 candidate
indexed the first 2,500 supported files under its default cap: 22,622 symbols
and 8,056 dependency edges. This is a bounded subset of that repository.

| Operation | Observed wall time |
| --- | ---: |
| Scan with a fresh database | 4.24 s |
| Cached scan, no source changes | 0.26 s |
| Repository map | 0.04 s |
| Impact query | 0.05 s |
| Hotspot report | 0.12 s |
| Visual graph data | 0.04 s |

Each operation ran once. The fresh scan used an empty database; operating-system
file caches were not flushed. These are observations on this machine and
repository, not a latency guarantee. Query timings exclude browser rendering,
Hermes startup, and model inference. The scan correctly reported `truncated: true`
and no Python parse errors. [Full measurements](evidence/benchmark-hermes.json).

Reproduce with a chosen workspace:

```bash
python3.11 scripts/benchmark_workspace.py /path/to/project \
  --target path/to/module.py --max-files 2500
```

The script uses a disposable database, does not change workspace files, and makes
no model calls. It emits JSON to stdout. Omit `--target` to skip the impact query.

Large-graph regressions separately cover a 2,500-node dependency chain, a
2,500-node cycle, distinct linked components, and self-edges beside a real cycle.
Cycle detection uses an explicit traversal stack, so it does not depend on
Python's recursion limit.
