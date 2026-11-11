#!/usr/bin/env python3
"""Interactive <60s live terminal showcase of Locus Codegraph for Hermes Agent.

Run instant verification:
    python3 scripts/run_demo.py

Run paced for screen recording (<60s X demo video):
    python3 scripts/run_demo.py --cinematic
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from locus_core import (  # noqa: E402
    analyze_impact,
    format_drift_banner,
    get_hotspots_report,
    get_repo_map,
    index_workspace,
    lookup_symbol_details,
    merge_drift_into_tool_result,
    reindex_edited_file,
)

# ANSI styling
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[38;5;81m"
GREEN = "\033[38;5;114m"
AMBER = "\033[38;5;214m"
RED = "\033[38;5;203m"
PURPLE = "\033[38;5;141m"
RESET = "\033[0m"


def _pause(seconds: float, cinematic: bool) -> None:
    if cinematic and seconds > 0:
        time.sleep(seconds)


def _banner(title: str, subtitle: str) -> None:
    line = "━" * 74
    print(f"\n{CYAN}{line}{RESET}")
    print(f"{BOLD}{CYAN}  ◈ {title}{RESET}  {DIM}— {subtitle}{RESET}")
    print(f"{CYAN}{line}{RESET}")


def _seed_demo_workspace(ws: Path) -> None:
    (ws / "core").mkdir(parents=True)
    (ws / "auth").mkdir(parents=True)
    (ws / "services").mkdir(parents=True)
    (ws / "api").mkdir(parents=True)
    (ws / "web").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)

    (ws / "core" / "crypto.py").write_text(
        '"""HMAC digest primitives."""\n'
        "import hashlib\n\n"
        "def sign_payload(raw: str, key: str = 'dev') -> str:\n"
        "    return hashlib.sha256(f'{key}:{raw}'.encode()).hexdigest()\n",
        encoding="utf-8",
    )
    (ws / "auth" / "tokens.py").write_text(
        '"""Session token verification."""\n'
        "from core.crypto import sign_payload\n\n"
        "def verify_token(token: str) -> bool:\n"
        '    """Verify bearer token signature."""\n'
        "    return bool(token and sign_payload(token))\n",
        encoding="utf-8",
    )
    (ws / "services" / "checkout.py").write_text(
        '"""Order settlement service."""\n'
        "from auth.tokens import verify_token\n"
        "from core.crypto import sign_payload\n\n"
        "def settle_order(token: str, cents: int) -> dict:\n"
        "    if not verify_token(token):\n"
        "        raise PermissionError('invalid token')\n"
        "    return {'receipt': sign_payload(str(cents)), 'cents': cents}\n",
        encoding="utf-8",
    )
    (ws / "api" / "gateway.py").write_text(
        '"""HTTP API entrypoint."""\n'
        "from services.checkout import settle_order\n\n"
        "def post_checkout(auth_header: str, amount_cents: int) -> dict:\n"
        "    return settle_order(auth_header, amount_cents)\n",
        encoding="utf-8",
    )
    (ws / "web" / "checkout_ui.ts").write_text(
        "export interface CheckoutPayload { amountCents: number; }\n"
        "export async function submitCheckout(token: string, payload: CheckoutPayload): Promise<boolean> {\n"
        "  return Boolean(token && payload.amountCents > 0);\n"
        "}\n",
        encoding="utf-8",
    )
    (ws / "tests" / "test_gateway.py").write_text(
        "from api.gateway import post_checkout\n\n"
        "def test_post_checkout_valid_token() -> None:\n"
        "    res = post_checkout('tok_live_123', 4900)\n"
        "    assert res['cents'] == 4900\n",
        encoding="utf-8",
    )


def run_showcase(cinematic: bool = False) -> int:
    print(f"\n{BOLD}{PURPLE}╔══════════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{PURPLE}║  LOCUS CODEGRAPH · Hermes Agent · AST Graph, Blast Radius & Drift Guard   ║{RESET}")
    print(f"{BOLD}{PURPLE}║  Zero Runtime Dependencies · CLI + @-Context + Web UI + Desktop SDK      ║{RESET}")
    print(f"{BOLD}{PURPLE}╚══════════════════════════════════════════════════════════════════════════╝{RESET}")
    _pause(1.2, cinematic)

    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir) / "acme-payments"
        db_path = Path(tmpdir) / "locus_demo.db"
        _seed_demo_workspace(ws)

        # ── ACT 1: Sub-25ms Indexing & PageRank Architectural Map ────────────
        _banner("ACT 1 · Incremental AST Indexing & PageRank Repo Map", "locus_map")
        stats = index_workspace(ws, db_path=db_path)
        rmap = get_repo_map(ws, max_files=6, db_path=db_path)
        print(
            f"  {GREEN}✓ Indexed workspace{RESET} {BOLD}{ws.name}{RESET} in "
            f"{BOLD}{stats['scan_ms']:.2f} ms{RESET} "
            f"({stats['file_count']} files · {stats['symbol_count']} symbols · {stats['edge_count']} edges)"
        )
        print(f"\n  {BOLD}Top Architectural Hubs (PageRank Centrality):{RESET}")
        for idx, mod in enumerate(rmap["modules"][:5], start=1):
            sigs = ", ".join(s["signature"] for s in mod["top_symbols"][:2])
            print(
                f"    {CYAN}{idx}.{RESET} {BOLD}{mod['rel_path']:<22}{RESET} "
                f"{DIM}[score={mod['pagerank']:.4f} · fan-in={mod['fan_in']}]{RESET}  {sigs}"
            )
        _pause(2.2, cinematic)

        # ── ACT 2: @sym: & @impact: Prompt Context + Transitive Blast Radius ─
        _banner(
            "ACT 2 · Inline @sym: & @impact: Context + Pre-Refactor Blast Radius",
            "ContextReferenceProvider + locus_impact",
        )
        print(
            f"  {PURPLE}User Prompt ❯{RESET} "
            f"\"Enforce tenant audience on {BOLD}{CYAN}@sym:verify_token{RESET} — "
            f"check {BOLD}{AMBER}@impact:auth/tokens.py{RESET} first\""
        )
        _pause(1.2, cinematic)

        sym = lookup_symbol_details(ws, "verify_token", db_path=db_path)
        impact = analyze_impact(ws, "auth/tokens.py", max_depth=3, db_path=db_path)

        print(f"\n  {GREEN}📌 Expanded @sym:verify_token:{RESET}")
        print(f"    • Defined at {BOLD}{sym['symbol']['rel_path']}:{sym['symbol']['line_start']}{RESET} → `{sym['symbol']['signature']}`")
        for caller in sym["callers"]:
            print(f"    • External caller: {BOLD}{caller['path']}:{caller['line']}{RESET} ({caller['arg_count']} arg passed)")

        print(f"\n  {AMBER}💥 Expanded @impact:auth/tokens.py:{RESET}")
        print(
            f"    • Risk Score: {BOLD}{AMBER}{impact['risk_level']} ({impact['impact_score']}/100){RESET}"
        )
        print(
            f"    • Hop-1 Direct Dependents:     {BOLD}{', '.join(d['rel_path'] for d in impact['direct_dependents'])}{RESET}"
        )
        print(
            f"    • Hop-2 Transitive Dependents: {BOLD}{', '.join(d['rel_path'] for d in impact['transitive_dependents'])}{RESET}"
        )
        print(
            f"    • Suggested Tests to Run:      {GREEN}{', '.join(t['rel_path'] for t in impact['covering_tests'])}{RESET}"
        )
        _pause(2.6, cinematic)

        # ── ACT 3: Real-Time Signature Drift Guard & Self-Healing ────────────
        _banner(
            "ACT 3 · Real-Time Signature Drift Guard & Auto-Resolution",
            "post_tool_call + transform_tool_result",
        )
        print(
            f"  {AMBER}⚙ Agent calls `patch` on auth/tokens.py:{RESET}\n"
            f"    {DIM}- def verify_token(token: str) -> bool:{RESET}\n"
            f"    {GREEN}+ def verify_token(token: str, audience: str) -> bool:{RESET}"
        )
        _pause(1.0, cinematic)

        (ws / "auth" / "tokens.py").write_text(
            '"""Session token verification."""\n'
            "from core.crypto import sign_payload\n\n"
            "def verify_token(token: str, audience: str) -> bool:\n"
            '    """Verify bearer token signature and tenant audience."""\n'
            "    return bool(token and audience and sign_payload(f'{audience}:{token}'))\n",
            encoding="utf-8",
        )
        t0 = time.perf_counter()
        drift_outcome = reindex_edited_file(ws, "auth/tokens.py", tool_name="patch", db_path=db_path)
        hook_ms = (time.perf_counter() - t0) * 1000.0
        banner_text = format_drift_banner(drift_outcome["drift_alerts"])
        merged_json = json.loads(
            merge_drift_into_tool_result(
                json.dumps({"status": "ok", "output": "Patched auth/tokens.py"}),
                banner_text,
            )
        )

        print(f"\n  {RED}⚡ Intercepted in {hook_ms:.2f} ms — Injected into Tool Result:{RESET}")
        for ln in merged_json["locus_drift_alert"].splitlines():
            print(f"    {RED}│{RESET} {ln}")
        _pause(2.4, cinematic)

        print(
            f"\n  {CYAN}⚙ Fixture script repairs caller in services/checkout.py:6:{RESET}\n"
            f"    {GREEN}+ if not verify_token(token, 'checkout'):{RESET}"
        )
        (ws / "services" / "checkout.py").write_text(
            '"""Order settlement service."""\n'
            "from auth.tokens import verify_token\n"
            "from core.crypto import sign_payload\n\n"
            "def settle_order(token: str, cents: int) -> dict:\n"
            "    if not verify_token(token, 'checkout'):\n"
            "        raise PermissionError('invalid token')\n"
            "    return {'receipt': sign_payload(str(cents)), 'cents': cents}\n",
            encoding="utf-8",
        )
        t1 = time.perf_counter()
        reindex_edited_file(ws, "services/checkout.py", tool_name="patch", db_path=db_path)
        heal_ms = (time.perf_counter() - t1) * 1000.0
        hotspots = get_hotspots_report(ws, db_path=db_path)

        print(
            f"  {GREEN}✓ Re-indexed services/checkout.py in {heal_ms:.2f} ms → "
            f"Open Drift Alerts: {len(hotspots['open_drift_alerts'])} (Auto-Resolved!){RESET}"
        )
        _pause(1.5, cinematic)

        # ── Summary Footer ───────────────────────────────────────────────────
        print(f"\n{CYAN}{'━' * 74}{RESET}")
        print(
            f"  {BOLD}Locus Codegraph Unified Surfaces:{RESET} "
            f"{GREEN}4 Agent Tools{RESET} · {CYAN}3 @-Context Prefixes{RESET} · "
            f"{AMBER}Web Dashboard (/locus-codegraph){RESET} · {PURPLE}Hermes Desktop SDK{RESET}"
        )
        print(f"{CYAN}{'━' * 74}{RESET}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Locus Codegraph live terminal showcase.")
    parser.add_argument(
        "--cinematic",
        action="store_true",
        help="Add natural pauses between acts for <60s screen recording.",
    )
    opts = parser.parse_args()
    return run_showcase(cinematic=opts.cinematic)


if __name__ == "__main__":
    raise SystemExit(main())
