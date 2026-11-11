# v0.1.5 stale-source interaction follow-up

**PASS — 11 November 2026**, candidate engine
`c622b20c86fdd87d9146f784f03e652ca7958abb601a65db582b4caf52eafc5c`.

This follow-up used the actual Hermes Desktop 0.22 macOS app, freshly relaunched,
and the restarted Web Dashboard with the same synthetic six-file fixture.
The earlier [app provenance and setup](v015-native-verification.md) still apply.
The verification agent operated the interfaces and inspected accessibility state
and screenshots in the conversation. No pixel assertions or standalone image
files were archived. Fixture edits used real Hermes CLI hooks with zero model calls.

| Action | Observed result |
| --- | --- |
| Add required `issuer` parameter through Hermes | One open alert with eight caller locations; [edit receipt](v015-native-stale-seed.json) |
| Append comments to `services/audits.py` beyond 512 KiB, leaving its calls unchanged; click native **Rescan** | One alert retained all eight records: two confirmed failures and six needing verification. Coverage opened as incomplete and displayed the source path and 512 KiB reason. The native collapsed caller list and coverage were visually inspected; [persisted state](v015-native-stale-after-rescan.json) records every caller |
| Reload Web Dashboard | The same stale-source warning persisted |
| Restore the readable original caller file; click Web **Scan** | Stale warning cleared, but one alert with eight confirmed incompatible callers remained. Native polling showed the same state |
| Restore the helper's original signature through Hermes | Zero open alerts, three closed records, no stale or parse diagnostics, and three fixture tests passing; [cleanup receipt](v015-native-stale-cleanup.json) |

Both final views showed zero open alerts and the instruction to run tests to
verify behavior. No repair draft was sent. This checks the selected oversized-file
workflow; it does not establish general application compatibility or model
performance. The 20-of-total stale-path rendering limit is covered separately
by the [component contract tests](v015-tests-stale-ui.txt).
