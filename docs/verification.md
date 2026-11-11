# Verification — Locus Codegraph

Verified on 11 November 2026 on macOS (Darwin 25.4.0, arm64).

The local checks below cover **0.1.3** after the reliability changes. The
[GitHub workflow](https://github.com/atikur/hermes-locus/actions/workflows/tests.yml)
separately runs the standalone suite on Python 3.11, 3.12, and 3.14.

Version 0.1.2 uses the distinct plugin identity `locus-codegraph` to coexist with
an unrelated plugin named Locus. The recorded model session below used 0.1.1;
its event transcript is preserved unchanged. The refactoring engine is unchanged
by the identity update.

## Reproducible checks

| Check | Observed result |
| --- | --- |
| Standalone Python 3.11.16, `requirements-dev.txt` | 63 tests run: 60 passed, 3 Hermes-specific checks skipped |
| Installed Hermes, Python 3.14.7 | 63 tests passed, no skips |
| `hermes plugins validate . --install-deps` | Passed; security scan safe; declarations match registrations |
| `hermes plugins doctor . --ci` | Passed; 4 tools and 3 hooks registered |
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
