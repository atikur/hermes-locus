# Native Desktop interaction record — v0.1.5

Observed on 11 November 2026 in the actual macOS arm64 Hermes Desktop app,
version 0.22.0. This is an interaction checklist recorded by the verification
agent from the app's UI state and rendered view, not an automated pixel test.
Fixture edits were dispatched separately through real Hermes file tools;
the repair context inserted into Desktop was left unsent.

The [distribution receipt](v015-native-artifact.json) records bundled app
artifact SHA-256
`8a3b64b4e2b10b88912937f883a2c616ea902f3b5c683d3cdffc459de7fa8f73`.
The app's installation stamp names build commit
`e06df9c2eae6901f4c88f464e0ae6e753c0428b8`, tag `v0.22.0`.
The final fixture receipts used candidate engine
`11f27f7730280de389e6d73b358b9ee6e0e291eece064cb92d0dd3162f3c551e`.
The downloaded artifact SHA-256 matched the vendor's stable distribution
manifest, and `codesign --verify` passed. [Structured check record](v015-native-verification.json).

| Interaction | Observed result |
| --- | --- |
| Load plugin and workspace | Locus 0.1.5 appeared with Agent and Desktop enabled; the 360px pane loaded the synthetic verification workspace |
| Inspect a breaking signature edit | The inbox showed all eight affected calls, including locations beyond the former six-row cutoff |
| Select a caller | Clicking the caller changed dependency focus to its source file |
| Use the graph with the keyboard | A graph node could be focused and activated from the keyboard |
| Read the native graph | Labels were readable in the 360px pane after increasing their size |
| Add repair context | The composer received a complete draft containing all eight caller locations; it was not submitted |
| Repair the source | The actual caller repair cleared the alert; all three fixture tests passed |
| Inspect invalid Python | The persisted parse warning remained visible after a Web Dashboard reload |
| Dismiss a new unresolved alert | Desktop showed zero open alerts and two closed records while the newly required `issuer` argument was still missing from callers; dismissal did not repair code |
| Confirm dismissal is not repair | The fixture tests still failed after dismissal, as expected; cleanup then restored all three passing tests |
| Inspect the Web inbox | All eight rows were reachable through the caller-list scroll; caller navigation changed focus, and Copy repair context reported success |
| Finish with current status | Both interfaces showed six files, 15 symbols, zero open alerts, two closed records, and complete coverage; the unsent native draft was cleared |

Selected fixture receipts: [initial edit](v015-native-edit-seed.json),
[successful repair](v015-native-edit-repair.json), and
[new issue used for dismissal](v015-native-dismiss-seed.json).
These are selected receipts, not a complete recording of every UI interaction.
The fixture is synthetic and is not evidence of unassisted model performance.

Setup exposed a host limitation: a plugin installed only in the selected
`locus-verification` profile rendered its pane, but a backend launched in
`default` rejected its REST requests. Installing and enabling Locus in the
backend's launch profile made the API available. Upstream host code was not
modified; profile data and the fixture workspace remained explicitly selected.

Inspection prompted UI corrections: larger native graph labels and an explicit
keyboard focus ring; “suggested tests” in the Web graph; empty-state wording
about unresolved alerts rather than incompatible callers; and generic rescan
acknowledgments so live coverage is the only source of parse/truncation status.
The shipped-bundle tests cover these corrections. A final app recheck confirmed
the precise empty-state text and generic rescan acknowledgment, with current
coverage after repair. Screenshots/UI snapshots were inspected in the verification
conversation; standalone image files were not archived. These observations do
not constitute broad device, accessibility, or pixel-regression certification.
