#!/usr/bin/env python3
"""
Integrations Tool — introspection surface for provisioned external-API access.

An "integration" is an HTTPS proxy endpoint (e.g. ``hub-{vm}.int.exe.xyz``)
whose auth header is injected server-side by the platform's proxy layer.
The agent calls the proxy URL; the proxy adds credentials; the target
upstream receives an authenticated request. The agent never sees or
handles the raw secret.

This tool answers: **what external services is my auth already wired up for?**

Agents should call ``integrations list`` BEFORE reasoning about how to
authenticate to any external API. If the capability is listed, use that
URL directly — auth is automatic. If it's missing, the correct behavior
is NOT to ask the user for a raw API key; it's to tell the user what you
were trying to do and ask them to request the capability from the
platform admin.

The manifest lives at ``<HERMES_HOME>/integrations.json`` and is written
at provision time by the hermes-provisioner. Format::

    {
      "integrations": [
        {"name": "hub", "url": "https://hub-sal.int.exe.xyz",
         "target": "https://hub.slate.ceo",
         "auth": "Authorization header injected server-side",
         "scope": "per-agent",
         "purpose": "Hub messaging and MCP"},
        ...
      ]
    }

Header values are NEVER written into the manifest — only header names.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

TOOL_NOTE = (
    "These integrations inject auth headers server-side. You never see or "
    "need the secret values — just call the URL. "
    "If you need a capability that isn't listed, tell your owner what you "
    "were trying to do and ask them to request it from the platform admin "
    "(email niyant@slate.ceo). NEVER ask your owner to paste a raw API "
    "key — that's not how auth flows on this platform."
)

EMPTY_NOTE = (
    "No integrations manifest found. Either none were provisioned for this "
    "VM, or the manifest file is missing. Tell your owner what capability "
    "you need and ask them to request it from the platform admin."
)


def _manifest_path() -> Path:
    return get_hermes_home() / "integrations.json"


def _load_manifest() -> Dict[str, Any]:
    p = _manifest_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", p, exc)
        return {}


def _list_integrations() -> Dict[str, Any]:
    data = _load_manifest()
    entries: List[Dict[str, Any]] = data.get("integrations", [])
    if not entries:
        return {"integrations": [], "note": EMPTY_NOTE}
    return {"integrations": entries, "note": TOOL_NOTE}


def _describe_integration(name: str) -> Dict[str, Any]:
    if not name:
        return {"error": "name is required for 'describe' action."}
    data = _load_manifest()
    entries: List[Dict[str, Any]] = data.get("integrations", [])
    match = next((e for e in entries if e.get("name") == name), None)
    if not match:
        available = [e.get("name") for e in entries]
        return {
            "error": f"No integration named {name!r}.",
            "available": available,
            "note": EMPTY_NOTE if not available else TOOL_NOTE,
        }
    return dict(match, note=TOOL_NOTE)


def integrations_tool(action: str = "list", name: str = "", **_) -> Dict[str, Any]:
    """Entry point. ``action`` is 'list' or 'describe'."""
    action = (action or "list").strip().lower()
    if action == "list":
        return _list_integrations()
    if action == "describe":
        return _describe_integration(name)
    return {"error": f"Unknown action {action!r}. Use: list, describe."}


# ---------------------------------------------------------------------------
# Tool schema + registry
# ---------------------------------------------------------------------------

INTEGRATIONS_SCHEMA = {
    "name": "integrations",
    "description": (
        "List external-API integrations provisioned for this VM. Each "
        "integration is a URL (e.g. https://hub-{vm}.int.exe.xyz) whose "
        "auth header is injected server-side by the platform proxy — you "
        "never see secret values. "
        "Call this BEFORE reasoning about how to authenticate to any "
        "external API. If the capability is listed, use the URL directly. "
        "If not, tell your owner what you were trying to do and ask them "
        "to request it from the platform admin. Never ask the owner to "
        "paste raw API keys."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "describe"],
                "description": (
                    "'list' returns every provisioned integration; "
                    "'describe' returns details on one by name."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Integration name (required for 'describe'). "
                    "Examples: 'hub', 'telegram', 'openai-embed'."
                ),
            },
        },
        "required": ["action"],
    },
}


def _check_integrations_requirements() -> bool:
    """Always available — just reads a local file."""
    return True


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="integrations",
    toolset="integrations",
    schema=INTEGRATIONS_SCHEMA,
    handler=lambda args, **kw: integrations_tool(
        action=args.get("action", "list"),
        name=args.get("name", ""),
    ),
    check_fn=_check_integrations_requirements,
    emoji="🔌",
)
