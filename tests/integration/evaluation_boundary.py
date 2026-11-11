"""Evaluation-only file boundary, installed identically in both study arms.

The model may inspect the public disposable checkout, never the installed
private plugin or evaluator profile. This is not part of the Locus product.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil


def check_call(tool_name, args, root):
    """Return None only for supported file calls wholly inside a real root."""
    blocked = {"action": "block", "message": "Evaluation file boundary: access is limited to the current disposable public checkout."}
    if not isinstance(args, dict) or not root:
        return blocked
    try:
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            return blocked
        if tool_name in {"read_file", "write_file"}:
            paths = [args.get("path")]
        elif tool_name == "search_files":
            paths = [args.get("path", ".")]
            raw = paths[0]
            if not isinstance(raw, str) or not raw.strip():
                return blocked
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = root / target
            if not target.resolve().is_relative_to(root):
                return blocked
            # Hermes recovers a nonexistent root containing commas/whitespace
            # by searching each component. Validate those exact fallback roots,
            # while preserving legitimate public multi-root requests.
            if not target.exists():
                parts = ([part.strip() for part in raw.split(",") if part.strip()]
                         if "," in raw else raw.split())
                if len(parts) > 1:
                    paths.extend(parts)
        elif tool_name == "patch" and args.get("mode", "replace") == "replace":
            paths = [args.get("path")]
        elif tool_name == "patch" and args.get("mode") == "patch":
            patch = args.get("patch")
            if not isinstance(patch, str):
                return blocked
            paths = []
            for line in patch.splitlines():
                if not line.lstrip().startswith("***"):
                    continue
                if line in {"*** Begin Patch", "*** End Patch", "*** End of File"}:
                    continue
                match = re.fullmatch(r"\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)", line)
                move = re.fullmatch(r"\*\*\* Move File: (.+?)\s*->\s*(.+)", line)
                if match:
                    paths.append(match.group(1).strip())
                elif move:
                    paths.extend(part.strip() for part in move.groups())
                else:
                    # Host accepts additional whitespace forms. Refuse the
                    # entire patch rather than silently skipping any header.
                    return blocked
            if not paths:
                return blocked
        else:
            return blocked
        for path in paths:
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                return blocked
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = root / target
            if not target.resolve().is_relative_to(root):
                return blocked
    except (OSError, RuntimeError, ValueError):
        return blocked
    return None


def _before(tool_name="", args=None, **_):
    return check_call(tool_name, args, os.environ.get("LOCUS_EVAL_ROOT"))


def register(ctx):
    ctx.register_hook("pre_tool_call", _before)


def install(home):
    plugin = Path(home) / "plugins/evaluation-boundary"
    plugin.mkdir(parents=True)
    shutil.copyfile(__file__, plugin / "__init__.py")
    (plugin / "plugin.yaml").write_text(
        'name: evaluation-boundary\nversion: "0.0.1"\n'
        'description: "Limit evaluation file access to the public disposable checkout."\n'
        'provides_hooks:\n  - pre_tool_call\n', encoding="utf-8")
