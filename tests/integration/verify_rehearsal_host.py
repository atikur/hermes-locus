#!/usr/bin/env python3
"""Exercise the rehearsal through a real Hermes host: plugin discovery, tool dispatch, @whatif expansion,
and applying the returned caller patch with Hermes's own patch tool. Zero model calls.

    /path/to/hermes/python verify_rehearsal_host.py <plugin-checkout> <hermes-source> <hermes-runtime>
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
plugin_repo, source, runtime = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
FILES = {
    "auth/__init__.py": "",
    "auth/tokens.py": "def verify_token(token, audience=None):\n    return token\n",
    "checkout.py": "from auth.tokens import verify_token\n\ndef charge(token):\n    return verify_token(\n        token,\n        audience='checkout',\n    )\n",
    "api.py": "import auth.tokens as tokens\nresult = tokens.verify_token('t', audience='api')\nplain = tokens.verify_token('t')\n",
}
with tempfile.TemporaryDirectory(prefix="locus-rehearsal-host-") as scratch:
    root = Path(scratch).resolve(); home = root / "home"; install = home / "plugins/locus-codegraph"; install.parent.mkdir(parents=True)
    names = subprocess.check_output(["git", "-C", str(plugin_repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).decode().split("\0")
    for name in filter(None, names):
        src = plugin_repo / name
        if src.is_file() and not name.startswith("docs/evidence/"):
            dest = install / name; dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dest)
    (home / "config.yaml").write_text("plugins:\n  enabled: [locus-codegraph]\n  hook_callback_timeout: 120\nterminal:\n  backend: local\n")
    ws = root / "ws"
    for rel, text in FILES.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True); (ws / rel).write_text(text)
    path = os.environ.get("PATH", os.defpath); os.environ.clear()
    os.environ.update({"PATH": path, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(runtime), "TERMINAL_ENV": "local",
                       "PYTHONDONTWRITEBYTECODE": "1", "TERMINAL_CWD": str(ws), "LOCUS_WORKSPACE": str(ws)})
    sys.dont_write_bytecode = True; sys.path.insert(0, str(source)); os.chdir(ws)
    from agent.runtime_cwd import set_session_cwd
    from hermes_cli import lifecycle, plugins
    from model_tools import handle_function_call
    plugins.discover_plugins()
    active = next(row for row in plugins.get_plugin_manager().list_plugins() if row["name"] == "locus-codegraph")
    assert active["enabled"] and not active["error"], active
    print("plugin loaded:", active.get("version"), "| tools:", active.get("tools"), "| hooks:", active.get("hooks"))
    set_session_cwd(str(ws)); lifecycle.invoke_hook("on_session_start", session_id="rehearsal")
    call = lambda name, args, cid: handle_function_call(name, args, session_id="rehearsal", task_id="rehearsal", turn_id="t", api_request_id="t", tool_call_id=cid)
    raw = call("locus_rehearse", {"target": "verify_token", "new_signature": "(token, scope=None)"}, "c1")
    result = json.loads(raw) if isinstance(raw, str) else raw
    print("1. tool ok:", result.get("ok"), "| would_break:", result.get("would_break"), "| patch files:", result.get("caller_patch_files"))
    assert result["ok"] and result["would_break"] == ["api.py:2", "checkout.py:4"], result
    # 2. @whatif expansion through Hermes's own reference parser
    from agent.context_references import parse_context_references, preprocess_context_references
    message = "Before editing, check @whatif:verify_token(token,*,scope=None) please."
    refs = parse_context_references(message)
    print("2. parsed refs:", [(r.kind, r.target) for r in refs])
    expanded = preprocess_context_references(message, cwd=str(ws), context_length=200000)
    text = getattr(expanded, "message", None) or str(expanded)
    print("   expansion mentions breaks:", "would break" in text, "| quoted form parsed:", [(r.kind, r.target) for r in parse_context_references('@whatif:"verify_token(token, *, scope=None)"')])
    assert "would break" in text, text[:600]
    # 3. Apply the returned caller patch with Hermes's patch tool, then make the definition change: expect no alert.
    applied = call("patch", {"mode": "patch", "patch": result["caller_patch"]}, "c2")
    applied = json.loads(applied) if isinstance(applied, str) else applied
    print("3. caller patch applied by Hermes:", applied.get("success"), "| files:", sorted(Path(p).name for p in applied.get("files_modified", [])), "| alert after caller patch:", bool(applied.get("locus_drift_alert")))
    assert applied.get("success"), applied
    assert "scope='checkout'" in (ws / "checkout.py").read_text() and "scope='api'" in (ws / "api.py").read_text()
    definition = "*** Begin Patch\n*** Update File: auth/tokens.py\n@@\n-def verify_token(token, audience=None):\n+def verify_token(token, scope=None):\n     return token\n*** End Patch"
    edited = call("patch", {"mode": "patch", "patch": definition}, "c3")
    edited = json.loads(edited) if isinstance(edited, str) else edited
    print("4. definition edit ok:", edited.get("success"), "| drift alert after rehearsed refactor:", bool(edited.get("locus_drift_alert")))
    assert edited.get("success") and not edited.get("locus_drift_alert"), edited
    again = json.loads(call("locus_rehearse", {"target": "verify_token", "new_signature": "(token, scope=None)"}, "c4"))
    print("5. rehearsing the now-current signature: would_break", again["would_break"], "compatible", again["compatible_count"])
    # 6. Definition-first: the edit alert itself carries the caller patch; applying it clears the alert.
    second = "*** Begin Patch\n*** Update File: auth/tokens.py\n@@\n-def verify_token(token, scope=None):\n+def verify_token(token, region=None):\n     return token\n*** End Patch"
    edited = call("patch", {"mode": "patch", "patch": second}, "c5")
    edited = json.loads(edited) if isinstance(edited, str) else edited
    alert = edited.get("locus_drift_alert") or ""
    print("6. alert names callers:", "Update caller `api.py:2`" in alert and "Update caller `checkout.py:4`" in alert, "| carries patch:", "Ready caller patch" in alert)
    carried = alert[alert.index("*** Begin Patch"):alert.index("*** End Patch") + len("*** End Patch")]
    fixed = call("patch", {"mode": "patch", "patch": carried}, "c6")
    fixed = json.loads(fixed) if isinstance(fixed, str) else fixed
    health = json.loads(call("locus_hotspots", {}, "c7"))
    print("7. carried patch applied:", fixed.get("success"), "| new alert:", bool(fixed.get("locus_drift_alert")), "| open alerts now:", health.get("open_drift_count"))
    assert fixed.get("success") and health.get("open_drift_count") == 0 and "region='checkout'" in (ws / "checkout.py").read_text()
    print("HOST CHECK PASSED")
