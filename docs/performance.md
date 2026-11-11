# Larger-workspace trial

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
