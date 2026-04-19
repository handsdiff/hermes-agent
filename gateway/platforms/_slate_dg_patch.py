"""dg-patch — route discord.py through hermes-provisioner's Discord transport.

What it changes at import time:

1. REST calls (``discord.http.Route.BASE``) → the per-agent HTTP integration
   ``https://discord-<vm>.int.exe.xyz/api/v10``. exe.dev injects
   ``Authorization: Bot <real-token>`` server-side.

2. ``HTTPClient.request`` runs with ``self.token = None`` so the client
   never attaches an ``Authorization`` header itself. Auth is purely
   transport-layer; this VM has no bot token on it.

3. Gateway WebSocket (``DiscordWebSocket.from_client`` + sharded
   ``HTTPClient.get_bot_gateway``) mints a fresh single-use ticket per
   connect via ``platform-<vm>.int.exe.xyz/discord-gateway/ticket`` and
   connects to ``wss://discord-gateway.slate.ceo/tkt/<ticket>``. dg-proxy
   on sf1 rewrites the IDENTIFY frame with the real bot token.

4. Gateway compression forced off — dg-proxy doesn't handle zlib-stream
   (could be added later; keep it simple for now).

Imported via ``sitecustomize.py`` in the venv so the monkey-patches apply
before hermes's own imports of discord.py.
"""

from __future__ import annotations

import logging
import os
import socket

try:
    import aiohttp
    import yarl
    import discord  # noqa: F401 — trigger submodule loads
    import discord.http
    import discord.gateway
except Exception:
    # If any of these fail we can't patch. Leave discord.py alone.
    raise

log = logging.getLogger("dg-patch")

_VM = os.environ.get("DG_PATCH_VM") or socket.gethostname()
_HTTP_BASE = f"https://discord-{_VM}.int.exe.xyz/api/v10"
_TICKET_URL = f"https://platform-{_VM}.int.exe.xyz/discord-gateway/ticket"
_WS_PUBLIC_BASE = "wss://discord-gateway.slate.ceo"

# --- 1. REST base URL ------------------------------------------------------
discord.http.Route.BASE = _HTTP_BASE

# --- 2. Never attach Authorization from the client. Auth is transport-layer.
_orig_request = discord.http.HTTPClient.request


async def _patched_request(self, route, *args, **kwargs):
    saved = self.token
    self.token = None
    try:
        return await _orig_request(self, route, *args, **kwargs)
    finally:
        self.token = saved


discord.http.HTTPClient.request = _patched_request


# --- 3. Gateway URL: mint a fresh ticket per connect ----------------------
async def _mint_ws_url() -> str:
    async with aiohttp.ClientSession() as s:
        async with s.post(_TICKET_URL) as r:
            r.raise_for_status()
            body = await r.json()
    return body["ws_url"]


_orig_from_client = discord.gateway.DiscordWebSocket.from_client.__func__


@classmethod
async def _patched_from_client(cls, client, *, gateway=None, compress=True, **kw):
    # dg-proxy currently runs plain JSON; force compress off so Discord
    # doesn't ship us zlib-stream frames we can't parse.
    compress = False
    if gateway is None:
        url = await _mint_ws_url()
        gateway = yarl.URL(url)
    return await _orig_from_client(
        cls, client, gateway=gateway, compress=compress, **kw
    )


discord.gateway.DiscordWebSocket.from_client = _patched_from_client


# --- 4. AutoSharded path uses HTTPClient.get_bot_gateway directly ---------
try:
    from discord.http import SessionStartLimit
except Exception:  # pragma: no cover — older discord.py
    SessionStartLimit = None


async def _patched_get_bot_gateway(self):
    ws_url = await _mint_ws_url()
    if SessionStartLimit is not None:
        limits = SessionStartLimit(
            total=1000, remaining=1000,
            reset_after=86_400_000, max_concurrency=1,
        )
        return 1, ws_url, limits
    return 1, ws_url


discord.http.HTTPClient.get_bot_gateway = _patched_get_bot_gateway


async def _patched_get_gateway(self, *, encoding="json", zlib=False, v=10):
    return await _mint_ws_url()


discord.http.HTTPClient.get_gateway = _patched_get_gateway

# --- 5. Fallback DEFAULT_GATEWAY points at our host (no ticket → 4401) ---
discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(_WS_PUBLIC_BASE + "/")

log.warning("dg-patch active vm=%s rest=%s ws=%s", _VM, _HTTP_BASE, _WS_PUBLIC_BASE)
