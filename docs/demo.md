# How the demo was recorded

[`assets/locus-v020-demo.mp4`](../assets/locus-v020-demo.mp4) is 71.6 seconds, 1920x1080, with narration. It shows one real session, recorded in one take on 11 November 2026.

## Setup

- **Hermes:** the official Hermes Agent app bundle's own Hermes, v0.22.0, serving its web dashboard on 127.0.0.1. The **Chat** page embeds the real Hermes terminal UI.
- **Plugin:** installed into a fresh profile with `hermes plugins install https://github.com/atikur/hermes-locus --enable`, which resolved to commit `43324066fccdb14a261d3b3964db2319a12ae005`.
- **Model:** gpt-5.4-mini, reasoning low, through the OpenAI API. The session used about 19.3k tokens.
- **Project:** the synthetic fixture from [`scripts/create_demo_workspace.py`](../scripts/create_demo_workspace.py).
- **Capture:** an isolated headless Chrome recorded with its DevTools screencast; only that page was captured. Keystrokes were sent as real key events by a script, so the prompt is exactly reproducible:

  > Change verify_token in auth/tokens.py to verify_token(token, *, audience) and accept only audience == "checkout". Edit only auth/tokens.py first, then say in one line what Locus reported in that edit's result. Then fix the callers it names and run: python3 -m unittest discover -s tests -v

## What happened

1. `/locus whatif verify_token(token, *, audience)` opened a Locus panel predicting that both callers would break. No model call, nothing edited.
2. Hermes edited `auth/tokens.py`. The patch tool's result carried the Locus alert naming `services/checkout.py:4` and `services/receipts.py:4` ([the actual tool result](evidence/v020-live-demo-session.json)).
3. Hermes reported what Locus found, fixed both callers and ran the tests, which passed. Running them again outside Hermes afterwards also passed.
4. `/locus status` showed zero open drift alerts, and the Locus dashboard tab showed one alert closed after the repair.

## Edits

- Title and end cards, and captions below the picture.
- Typing and the agent's work are sped up 2x, tagged "2x" on screen; the agent's reply plays at half speed so it can be read.
- Two zoomed crops: the rehearsal panel and the agent's reply. Two short frame holds let narration lines finish. Nothing is cut or reordered.
- Narration was added afterwards with text-to-speech.

## Limit

The Hermes terminal shows tool calls and the model's reply, not the raw tool result, so the alert itself is not on screen. The session record linked above shows it.
