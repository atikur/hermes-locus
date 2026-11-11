# Locus Codegraph — submission and demonstration

**Locus Codegraph shows Hermes what a change could break, then flags incompatible callers while it works.**

It adds a local structural code graph to Hermes Agent. Before a refactor, `@sym:`
and `@impact:` expose definitions, callers, dependent modules, and suggested tests.
After supported edits, its signature-drift guard puts actionable caller warnings
in tool results so Hermes can repair them. Python receives AST analysis and
checks positional and keyword binding for supported direct imports. TypeScript,
JavaScript, Go, and Rust receive lighter structural indexing.
The same index powers four agent tools, commands, a Web Dashboard, and a Desktop
pane. The analysis core uses Python's standard library and requires no hosted
index or additional model call.

## What makes it a plugin

Locus Codegraph persists a workspace index, subscribes to Hermes lifecycle hooks,
transforms tool results, registers context-reference providers, and exposes web
and desktop interfaces. The distinctive capability is structural feedback arriving
inside the editing loop. The plugin supplies evidence; Hermes performs repairs.

## Keyword rename replay

Locus Codegraph 0.1.4 ran a controlled replay of a same-arity keyword rename on
Hermes Agent's own source. Both versions received the same patch. The earlier
version reported zero affected calls. The new version located two gateway calls
and three test calls in the patch result and suggested replacing `max_len=`
with `limit=`. After the scripted repair, all 26 upstream tests passed and the
alert cleared. [Replay commands and results](verification.md#keyword-rename-on-hermes-agent)

## Recorded model session

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

- [Public repository](https://github.com/atikur/hermes-locus)
- [Release CI](https://github.com/atikur/hermes-locus/actions/workflows/tests.yml)
- [Catalog PR search](https://github.com/NousResearch/hermes-agent/pulls?q=is%3Apr+author%3Aatikur+locus-codegraph)
- [Official entry form](https://form.typeform.com/to/FwJLvOln)
- [Organizer announcement](https://x.com/NousResearch/status/2118094256438936062)
- [Catalog admission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)

The catalog entry must pin the published plugin's exact 40-character commit SHA.
Catalog submission and hackathon registration are separate: the repository and
PR do not establish that the entry form was submitted. The author will publish
the video on X tagging `@NousResearch`, then provide that post and the catalog PR
in the official form. Catalog listing remains subject to maintainer review.

## Suggested X post

> Built Locus Codegraph for the @NousResearch Hermes Agent Plugin Hackathon. It shows affected callers, flags signature drift inside edit results, and tracks the agent's repairs. Local analysis, no hosted index. Working demo below. https://github.com/atikur/hermes-locus
