"""Honcho memory plugin — MemoryProvider for Honcho AI-native memory.

Provides cross-session user modeling with dialectic Q&A, semantic search,
peer cards, and persistent conclusions via the Honcho SDK. Honcho provides AI-native cross-session user
modeling with dialectic Q&A, semantic search, peer cards, and conclusions.

The 4 tools (profile, search, context, conclude) are exposed through
the MemoryProvider interface.

Config: Uses the existing Honcho config chain:
  1. $HERMES_HOME/honcho.json (profile-scoped)
  2. ~/.honcho/config.json (legacy global)
  3. Environment variables
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (moved from tools/honcho_tools.py)
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "honcho_profile",
    "description": (
        "Retrieve or update a peer card from Honcho — a curated list of key facts "
        "about that peer (name, role, preferences, communication style, patterns). "
        "Pass `card` to update; omit `card` to read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {
                "type": "string",
                "description": "Peer to query. Built-in aliases: 'user' (default), 'ai'. Or pass any peer ID from this workspace.",
            },
            "card": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New peer card as a list of fact strings. Omit to read the current card.",
            },
        },
        "required": [],
    },
}

SEARCH_SCHEMA = {
    "name": "honcho_search",
    "description": (
        "Semantic search over Honcho's stored context about a peer. "
        "Returns raw excerpts ranked by relevance — no LLM synthesis. "
        "Cheaper and faster than honcho_reasoning. "
        "Good when you want to find specific past facts and reason over them yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for in Honcho's memory.",
            },
            "max_tokens": {
                "type": "integer",
                "description": "Token budget for returned context (default 800, max 2000).",
            },
            "peer": {
                "type": "string",
                "description": "Peer to query. Built-in aliases: 'user' (default), 'ai'. Or pass any peer ID from this workspace.",
            },
        },
        "required": ["query"],
    },
}

REASONING_SCHEMA = {
    "name": "honcho_reasoning",
    "description": (
        "Ask Honcho a natural language question and get a synthesized answer. "
        "Uses Honcho's LLM (dialectic reasoning) — higher cost than honcho_profile or honcho_search. "
        "Can query about any peer via alias or explicit peer ID. "
        "Pass reasoning_level to control depth: minimal (fast/cheap), low (default), "
        "medium, high, max (deep/expensive). Omit for configured default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A natural language question.",
            },
            "reasoning_level": {
                "type": "string",
                "description": (
                    "Override the default reasoning depth. "
                    "Omit to use the configured default (typically low). "
                    "Guide:\n"
                    "- minimal: quick factual lookups (name, role, simple preference)\n"
                    "- low: straightforward questions with clear answers\n"
                    "- medium: multi-aspect questions requiring synthesis across observations\n"
                    "- high: complex behavioral patterns, contradictions, deep analysis\n"
                    "- max: thorough audit-level analysis, leave no stone unturned"
                ),
                "enum": ["minimal", "low", "medium", "high", "max"],
            },
            "peer": {
                "type": "string",
                "description": "Peer to query. Built-in aliases: 'user' (default), 'ai'. Or pass any peer ID from this workspace.",
            },
        },
        "required": ["query"],
    },
}

CONTEXT_SCHEMA = {
    "name": "honcho_context",
    "description": (
        "Retrieve full session context from Honcho — summary, peer representation, "
        "peer card, and recent messages. No LLM synthesis. "
        "Cheaper than honcho_reasoning. Use this to see what Honcho knows about "
        "the current conversation and the specified peer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional focus query to filter context. Omit for full session context snapshot.",
            },
            "peer": {
                "type": "string",
                "description": "Peer to query. Built-in aliases: 'user' (default), 'ai'. Or pass any peer ID from this workspace.",
            },
        },
        "required": [],
    },
}

CONCLUDE_SCHEMA = {
    "name": "honcho_conclude",
    "description": (
        "Write or delete a conclusion about a peer in Honcho's memory. "
        "Conclusions are persistent facts that build a peer's profile. "
        "You MUST pass exactly one of: `conclusion` (to create) or `delete_id` (to delete). "
        "Passing neither is an error. "
        "Deletion is only for PII removal — Honcho self-heals incorrect conclusions over time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {
                "type": "string",
                "description": "A factual statement to persist. Provide this when creating a conclusion. Do not send it together with delete_id.",
            },
            "delete_id": {
                "type": "string",
                "description": "Conclusion ID to delete for PII removal. Provide this when deleting a conclusion. Do not send it together with conclusion.",
            },
            "peer": {
                "type": "string",
                "description": "Peer to query. Built-in aliases: 'user' (default), 'ai'. Or pass any peer ID from this workspace.",
            },
        },
        "required": [],
    },
}


ALL_TOOL_SCHEMAS = [PROFILE_SCHEMA, SEARCH_SCHEMA, REASONING_SCHEMA, CONTEXT_SCHEMA, CONCLUDE_SCHEMA]


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HonchoMemoryProvider(MemoryProvider):
    """Honcho AI-native memory with dialectic Q&A and persistent user modeling."""

    def __init__(self):
        self._manager = None   # HonchoSessionManager
        self._config = None    # HonchoClientConfig
        self._session_key = ""
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None

        # B1: recall_mode — set during initialize from config
        self._recall_mode = "hybrid"  # "context", "tools", or "hybrid"

        # Base context cache — refreshed on context_cadence, not frozen
        self._base_context_cache: Optional[str] = None
        self._base_context_cache_by_actor: dict[tuple[str, str, str], str] = {}
        self._base_context_lock = threading.Lock()

        # B5: Cost-awareness turn counting and cadence
        self._turn_count = 0
        self._injection_frequency = "every-turn"  # or "first-turn"
        self._context_cadence = 1   # minimum turns between context API calls
        self._dialectic_cadence = 1  # backwards-compat fallback; wizard writes 2 on new configs
        self._dialectic_depth = 1   # how many .chat() calls per dialectic cycle (1-3)
        self._dialectic_depth_levels: list[str] | None = None  # per-pass reasoning levels
        self._reasoning_heuristic: bool = True  # scale base level by query length
        self._reasoning_level_cap: str = "high"  # ceiling for auto-selected level
        self._last_context_turn = -999
        self._last_dialectic_turn = -999
        self._last_context_turn_by_actor: dict[tuple[str, str, str], int] = {}
        self._last_dialectic_turn_by_actor: dict[tuple[str, str, str], int] = {}

        # Liveness + observability state
        self._prefetch_thread_started_at: float = 0.0   # monotonic ts of current thread
        self._prefetch_thread_started_at_by_actor: dict[tuple[str, str, str], float] = {}
        self._prefetch_result_fired_at: int = -999      # turn the pending result was fired at
        self._prefetch_results_by_actor: dict[tuple[str, str, str], tuple[str, int]] = {}
        self._dialectic_empty_streak: int = 0           # consecutive empty returns
        self._dialectic_empty_streak_by_actor: dict[tuple[str, str, str], int] = {}
        self._prefetch_threads_by_actor: dict[tuple[str, str, str], threading.Thread] = {}

        # Port #1957: lazy session init for tools-only mode
        self._session_initialized = False
        self._lazy_init_kwargs: Optional[dict] = None
        self._lazy_init_session_id: Optional[str] = None
        self._actor_runtime_enabled = False
        self._actor_context: dict[str, Any] = {}


    @property
    def name(self) -> str:
        return "honcho"

    def is_available(self) -> bool:
        """Check if Honcho is configured. No network calls."""
        try:
            from plugins.memory.honcho.client import HonchoClientConfig
            cfg = HonchoClientConfig.from_global_config()
            # Port #2645: baseUrl-only verification — api_key OR base_url suffices
            return cfg.enabled and bool(cfg.api_key or cfg.base_url)
        except Exception:
            return False

    def save_config(self, values, hermes_home):
        """Write config to $HERMES_HOME/honcho.json (Honcho SDK native format)."""
        import json
        from pathlib import Path
        config_path = Path(hermes_home) / "honcho.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2))

    def get_config_schema(self):
        return [
            {"key": "api_key", "description": "Honcho API key", "secret": True, "env_var": "HONCHO_API_KEY", "url": "https://app.honcho.dev"},
            {"key": "baseUrl", "description": "Honcho base URL (for self-hosted)"},
        ]

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Run the full Honcho setup wizard after provider selection."""
        import types
        from plugins.memory.honcho.cli import cmd_setup
        cmd_setup(types.SimpleNamespace())

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize Honcho session manager.

        Handles: recall_mode, session name resolution,
        peer memory mode, SOUL.md ai_peer sync, memory file migration,
        and pre-warming context at init.
        """
        try:
            from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client
            from plugins.memory.honcho.session import HonchoSessionManager

            cfg = HonchoClientConfig.from_global_config()
            if not cfg.enabled or not (cfg.api_key or cfg.base_url):
                logger.debug("Honcho not configured — plugin inactive")
                return

            self._actor_context = dict(kwargs.get("actor_context") or {})
            self._actor_runtime_enabled = self._is_actor_runtime_enabled(cfg)

            # Gateway user_id is passed separately as runtime_user_peer_name.
            # Do not mutate cfg.peer_name: explicit config remains stable, and
            # actor-runtime mode uses structured per-turn actor_context.


            self._config = cfg
            self._legacy_peer_ids = kwargs.get("legacy_peer_ids") or []

            # ----- B1: recall_mode from config -----
            self._recall_mode = cfg.recall_mode  # "context", "tools", or "hybrid"
            logger.debug("Honcho recall_mode: %s", self._recall_mode)

            # ----- B5: cost-awareness config -----
            try:
                raw = cfg.raw or {}
                self._injection_frequency = raw.get("injectionFrequency", "every-turn")
                self._context_cadence = int(raw.get("contextCadence", 1))
                # Backwards-compat: unset dialecticCadence falls back to 1
                # (every turn) so existing honcho.json configs without the key
                # behave as they did before. New setups via `hermes honcho setup`
                # get dialecticCadence=2 written explicitly by the wizard.
                self._dialectic_cadence = int(raw.get("dialecticCadence", 1))
                self._dialectic_depth = max(1, min(cfg.dialectic_depth, 3))
                self._dialectic_depth_levels = cfg.dialectic_depth_levels
                self._reasoning_heuristic = cfg.reasoning_heuristic
                if cfg.reasoning_level_cap in self._LEVEL_ORDER:
                    self._reasoning_level_cap = cfg.reasoning_level_cap
            except Exception as e:
                logger.debug("Honcho cost-awareness config parse error: %s", e)

            # ----- Port #1969: aiPeer sync from SOUL.md — REMOVED -----
            # SOUL.md is persona content, not identity config. aiPeer should
            # only come from honcho.json (host block or root) or the default.
            # See scratch/memory-plugin-ux-specs.md #10 for rationale.

            # ----- Port #1957: lazy session init for tools-only mode -----
            if self._recall_mode == "tools":
                if cfg.init_on_session_start:
                    # Eager init even in tools mode (opt-in)
                    self._do_session_init(cfg, session_id, **kwargs)
                    return
                # Defer actual session creation until first tool call
                self._lazy_init_kwargs = kwargs
                self._lazy_init_session_id = session_id
                # Still need a client reference for _ensure_session
                self._config = cfg
                logger.debug("Honcho tools-only mode — deferring session init until first tool call")
                return

            # ----- Eager init (context or hybrid mode) -----
            self._do_session_init(cfg, session_id, **kwargs)

        except ImportError:
            logger.debug("honcho-ai package not installed — plugin inactive")
        except Exception as e:
            logger.warning("Honcho init failed: %s", e)
            self._manager = None

    @staticmethod
    def _truthy(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _is_actor_runtime_enabled(self, cfg) -> bool:
        raw = cfg.raw or {}
        host_raw = (raw.get("hosts") or {}).get(getattr(cfg, "host", "hermes"), {}) or {}
        return (
            self._truthy(host_raw.get("actorRuntime"))
            or self._truthy(host_raw.get("actorRuntimeEnabled"))
            or self._truthy(raw.get("actorRuntime"))
            or self._truthy(raw.get("actorRuntimeEnabled"))
            or self._truthy(os.getenv("HERMES_HONCHO_ACTOR_RUNTIME", ""))
        )

    def set_actor_context(self, actor_context: dict[str, Any] | None) -> None:
        """Update the current gateway actor for cached-agent turns."""
        self._actor_context = dict(actor_context or {})
        if self._manager and self._actor_runtime_enabled:
            setter = getattr(self._manager, "set_actor_context", None)
            if callable(setter):
                setter(self._actor_context)

    def _turn_actor_context(self, actor_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return an immutable per-operation actor snapshot."""
        return dict(actor_context if actor_context is not None else (self._actor_context or {}))

    def _actor_cache_key(
        self,
        actor_context: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        """Actor-scoped cache key for shared gateway sessions."""
        ctx = self._turn_actor_context(actor_context)
        if not self._actor_runtime_enabled:
            return (self._session_key or "", "legacy", "legacy")
        actor_peer = ""
        if self._manager:
            try:
                actor_peer = self._manager.current_actor_peer_id(actor_context=ctx)
            except Exception:
                actor_peer = ""
        if not actor_peer:
            actor_peer = str(
                ctx.get("peer_id")
                or ctx.get("person_id")
                or ctx.get("platform_user_id")
                or "system-unknown"
            )
        agent_peer = str(ctx.get("agent_peer_id") or "agent-unknown")
        return (self._session_key or "", actor_peer, agent_peer)

    def _actor_kwargs(self, actor_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Manager kwargs for actor-aware calls, preserving legacy expectations."""
        if not self._actor_runtime_enabled:
            return {}
        return {"actor_context": self._turn_actor_context(actor_context)}

    def _get_last_context_turn(self, actor_context: dict[str, Any] | None = None) -> int:
        if not self._actor_runtime_enabled:
            return self._last_context_turn
        return self._last_context_turn_by_actor.get(self._actor_cache_key(actor_context), -999)

    def _set_last_context_turn(self, value: int, actor_context: dict[str, Any] | None = None) -> None:
        if not self._actor_runtime_enabled:
            self._last_context_turn = value
            return
        self._last_context_turn_by_actor[self._actor_cache_key(actor_context)] = value

    def _get_last_dialectic_turn(self, actor_context: dict[str, Any] | None = None) -> int:
        if not self._actor_runtime_enabled:
            return self._last_dialectic_turn
        return self._last_dialectic_turn_by_actor.get(self._actor_cache_key(actor_context), -999)

    def _set_last_dialectic_turn(self, value: int, actor_context: dict[str, Any] | None = None) -> None:
        if not self._actor_runtime_enabled:
            self._last_dialectic_turn = value
            return
        self._last_dialectic_turn_by_actor[self._actor_cache_key(actor_context)] = value

    def _get_empty_streak(self, actor_context: dict[str, Any] | None = None) -> int:
        if not self._actor_runtime_enabled:
            return self._dialectic_empty_streak
        return self._dialectic_empty_streak_by_actor.get(self._actor_cache_key(actor_context), 0)

    def _set_empty_streak(self, value: int, actor_context: dict[str, Any] | None = None) -> None:
        if not self._actor_runtime_enabled:
            self._dialectic_empty_streak = value
            return
        self._dialectic_empty_streak_by_actor[self._actor_cache_key(actor_context)] = value

    def _increment_empty_streak(self, actor_context: dict[str, Any] | None = None) -> None:
        self._set_empty_streak(self._get_empty_streak(actor_context) + 1, actor_context)

    def _get_prefetch_thread(
        self,
        actor_context: dict[str, Any] | None = None,
    ) -> tuple[threading.Thread | None, float]:
        if not self._actor_runtime_enabled:
            return self._prefetch_thread, self._prefetch_thread_started_at
        key = self._actor_cache_key(actor_context)
        return (
            self._prefetch_threads_by_actor.get(key),
            self._prefetch_thread_started_at_by_actor.get(key, 0.0),
        )

    def _set_prefetch_thread(
        self,
        thread: threading.Thread,
        started_at: float,
        actor_context: dict[str, Any] | None = None,
    ) -> None:
        if not self._actor_runtime_enabled:
            self._prefetch_thread = thread
            self._prefetch_thread_started_at = started_at
            return
        key = self._actor_cache_key(actor_context)
        self._prefetch_threads_by_actor[key] = thread
        self._prefetch_thread_started_at_by_actor[key] = started_at

    def _get_base_context_cache(self, actor_context: dict[str, Any] | None = None) -> Optional[str]:
        if not self._actor_runtime_enabled:
            return self._base_context_cache
        return self._base_context_cache_by_actor.get(self._actor_cache_key(actor_context))

    def _set_base_context_cache(self, value: str, actor_context: dict[str, Any] | None = None) -> None:
        if not self._actor_runtime_enabled:
            self._base_context_cache = value
            return
        self._base_context_cache_by_actor[self._actor_cache_key(actor_context)] = value

    def _pop_prefetch_result(
        self,
        actor_context: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        if not self._actor_runtime_enabled:
            with self._prefetch_lock:
                result = self._prefetch_result
                fired_at = self._prefetch_result_fired_at
                self._prefetch_result = ""
                self._prefetch_result_fired_at = -999
            return result, fired_at
        key = self._actor_cache_key(actor_context)
        with self._prefetch_lock:
            return self._prefetch_results_by_actor.pop(key, ("", -999))

    def _store_prefetch_result(
        self,
        result: str,
        fired_at: int,
        actor_context: dict[str, Any] | None = None,
    ) -> None:
        if not result:
            return
        if not self._actor_runtime_enabled:
            with self._prefetch_lock:
                self._prefetch_result = result
                self._prefetch_result_fired_at = fired_at
            return
        key = self._actor_cache_key(actor_context)
        with self._prefetch_lock:
            self._prefetch_results_by_actor[key] = (result, fired_at)

    def _do_session_init(self, cfg, session_id: str, **kwargs) -> None:
        """Shared session initialization logic for both eager and lazy paths."""
        from plugins.memory.honcho.client import get_honcho_client
        from plugins.memory.honcho.session import HonchoSessionManager

        client = get_honcho_client(cfg)
        self._manager = HonchoSessionManager(
            honcho=client,
            config=cfg,
            context_tokens=cfg.context_tokens,
            runtime_user_peer_name=kwargs.get("user_id") or None,
            actor_runtime_enabled=self._actor_runtime_enabled,
            actor_context=self._actor_context,
        )

        # ----- B3: resolve_session_name -----
        session_title = kwargs.get("session_title")
        gateway_session_key = kwargs.get("gateway_session_key")
        self._session_key = (
            cfg.resolve_session_name(
                session_title=session_title,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
            )
            or session_id
            or "hermes-default"
        )
        logger.debug("Honcho session key resolved: %s", self._session_key)

        # Create session eagerly
        session = self._manager.get_or_create(
            self._session_key,
            actor_context=self._actor_context,
        )
        self._session_initialized = True

        # ----- B6: Memory file migration (one-time, for new sessions) -----
        # Skip under per-session strategy: every Hermes run creates a fresh
        # Honcho session by design, so uploading MEMORY.md/USER.md/SOUL.md to
        # each one would flood the backend with short-lived duplicates instead
        # of performing a one-time migration.
        try:
            if self._actor_runtime_enabled:
                logger.debug(
                    "Honcho memory file migration skipped: actor runtime uses per-actor peers (%s)",
                    self._session_key,
                )
            elif not session.messages and cfg.session_strategy != "per-session":
                from hermes_constants import get_hermes_home
                mem_dir = str(get_hermes_home() / "memories")
                self._manager.migrate_memory_files(self._session_key, mem_dir)
                logger.debug("Honcho memory file migration attempted for new session: %s", self._session_key)
            elif cfg.session_strategy == "per-session":
                logger.debug(
                    "Honcho memory file migration skipped: per-session strategy creates a fresh session per run (%s)",
                    self._session_key,
                )
        except Exception as e:
            logger.debug("Honcho memory file migration skipped: %s", e)

        # ----- B7: Pre-warming at init -----
        # Context prewarm warms peer.context() (base layer), consumed via
        # pop_context_result() in prefetch(). Dialectic prewarm runs the
        # full configured depth and writes into _prefetch_result so turn 1
        # consumes the result directly.
        if self._recall_mode in ("context", "hybrid"):
            try:
                self._manager.prefetch_context(
                    self._session_key,
                    actor_context=self._actor_context,
                )
            except Exception as e:
                logger.debug("Honcho context prewarm failed: %s", e)

            _prewarm_query = (
                "Summarize what you know about the current speaker. "
                "Focus on preferences, current projects, and working style."
            )

            def _prewarm_dialectic() -> None:
                try:
                    r = self._run_dialectic_depth(
                        _prewarm_query,
                        actor_context=self._actor_context,
                    )
                except Exception as exc:
                    logger.debug("Honcho dialectic prewarm failed: %s", exc)
                    self._increment_empty_streak(self._actor_context)
                    return
                if r and r.strip():
                    self._store_prefetch_result(
                        r,
                        0,
                        actor_context=self._actor_context,
                    )
                    # Treat prewarm as turn 0 so cadence gating starts clean.
                    self._set_last_dialectic_turn(0, self._actor_context)
                    self._set_empty_streak(0, self._actor_context)
                else:
                    self._increment_empty_streak(self._actor_context)

            _started_at = time.monotonic()
            _thread = threading.Thread(
                target=_prewarm_dialectic, daemon=True, name="honcho-prewarm-dialectic"
            )
            self._set_prefetch_thread(_thread, _started_at, self._actor_context)
            _thread.start()
            logger.debug("Honcho pre-warm started for session: %s", self._session_key)

    def _ensure_session(self) -> bool:
        """Lazily initialize the Honcho session (for tools-only mode).

        Returns True if the manager is ready, False otherwise.
        """
        if self._manager and self._session_initialized:
            return True
        if not self._config or not self._lazy_init_kwargs:
            return False

        try:
            self._do_session_init(
                self._config,
                self._lazy_init_session_id or "hermes-default",
                **self._lazy_init_kwargs,
            )
            # Clear lazy refs
            self._lazy_init_kwargs = None
            self._lazy_init_session_id = None
            return self._manager is not None
        except Exception as e:
            logger.warning("Honcho lazy session init failed: %s", e)
            return False

    def _format_first_turn_context(self, ctx: dict) -> str:
        """Format the prefetch context dict into a readable system prompt block."""
        parts = []

        # Session summary — session-scoped context, placed first for relevance
        summary = ctx.get("summary", "")
        if summary:
            parts.append(f"## Session Summary\n{summary}")

        rep = ctx.get("representation", "")
        if rep:
            actor_label = "Current Speaker Representation" if ctx.get("actor_peer_id") else "User Representation"
            actor_meta = ""
            if ctx.get("actor_peer_id"):
                display = ctx.get("actor_display_name") or ctx.get("actor_peer_id")
                kind = ctx.get("actor_kind") or "peer"
                actor_meta = f" ({kind}: {display})"
            parts.append(f"## {actor_label}{actor_meta}\n{rep}")

        # Legacy representations from pre-unification transport-level peers.
        # Merged alongside the primary so the model sees full owner history.
        legacy = ctx.get("legacy_representations", "")
        if legacy:
            parts.append(f"## Prior Channel History\n{legacy}")

        card = ctx.get("card", "")
        if card:
            card_label = "Current Speaker Peer Card" if ctx.get("actor_peer_id") else "User Peer Card"
            parts.append(f"## {card_label}\n{card}")

        ai_rep = ctx.get("ai_representation", "")
        if ai_rep:
            parts.append(f"## AI Self-Representation\n{ai_rep}")

        ai_card = ctx.get("ai_card", "")
        if ai_card:
            parts.append(f"## AI Identity Card\n{ai_card}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    def system_prompt_block(self) -> str:
        """Return system prompt text, adapted by recall_mode.

        Returns only the mode header and tool instructions — static text
        that doesn't change between turns (prompt-cache friendly).
        Live context (representation, card) is injected via prefetch().
        """
        if not self._manager or not self._session_key:
            # tools-only mode without session yet still returns a minimal block
            if self._recall_mode == "tools" and self._config:
                return (
                    "# Honcho Memory\n"
                    "Active (tools-only mode). Use honcho_profile, honcho_search, "
                    "honcho_reasoning, honcho_context, and honcho_conclude tools to access user memory."
                )
            return ""

        # ----- B1: adapt text based on recall_mode -----
        if self._recall_mode == "context":
            header = (
                "# Honcho Memory\n"
                "Active (context-injection mode). Relevant user context is automatically "
                "injected before each turn. No memory tools are available — context is "
                "managed automatically."
            )
        elif self._recall_mode == "tools":
            header = (
                "# Honcho Memory\n"
                "Active (tools-only mode). Use honcho_profile for a quick factual snapshot, "
                "honcho_search for raw excerpts, honcho_context for raw peer context, "
                "honcho_reasoning for synthesized answers (pass reasoning_level "
                "minimal/low/medium/high/max — you pick the depth per call), "
                "honcho_conclude to save facts about the user. "
                "No automatic context injection — you must use tools to access memory."
            )
        else:  # hybrid
            header = (
                "# Honcho Memory\n"
                "Active (hybrid mode). Relevant context is auto-injected AND memory tools are available. "
                "Use honcho_profile for a quick factual snapshot, "
                "honcho_search for raw excerpts, honcho_context for raw peer context, "
                "honcho_reasoning for synthesized answers (pass reasoning_level "
                "minimal/low/medium/high/max — you pick the depth per call), "
                "honcho_conclude to save facts about the user."
            )

        return header

    def prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        actor_context: dict[str, Any] | None = None,
    ) -> str:
        """Return base context (representation + card) plus dialectic supplement.

        Assembles two layers:
        1. Base context from peer.context() — cached, refreshed on context_cadence
        2. Dialectic supplement — cached, refreshed on dialectic_cadence

        B1: Returns empty when recall_mode is "tools" (no injection).
        B5: Respects injection_frequency — "first-turn" returns cached/empty after turn 0.
        Port #3265: Truncates to context_tokens budget.
        """
        # B1: tools-only mode — no auto-injection
        if self._recall_mode == "tools":
            return ""

        # B5: injection_frequency — if "first-turn" and past first turn, return empty.
        # _turn_count is 1-indexed (first user message = 1), so > 1 means "past first".
        if self._injection_frequency == "first-turn" and self._turn_count > 1:
            return ""

        # Trivial prompts ("ok", "yes", slash commands) carry no semantic signal.
        if self._is_trivial_prompt(query):
            return ""

        actor_context_snapshot = self._turn_actor_context(actor_context)
        parts = []

        # ----- Layer 1: Base context (representation + card) -----
        # On first call, fetch synchronously so turn 1 isn't empty.
        # After that, serve from cache and refresh in background on cadence.
        with self._base_context_lock:
            cached_context = self._get_base_context_cache(actor_context_snapshot)
            if cached_context is None:
                # First call — synchronous fetch
                try:
                    ctx = self._manager.get_prefetch_context(
                        self._session_key,
                        legacy_peer_ids=self._legacy_peer_ids,
                        actor_context=actor_context_snapshot,
                    )
                    self._set_base_context_cache(
                        self._format_first_turn_context(ctx) if ctx else "",
                        actor_context_snapshot,
                    )
                    self._set_last_context_turn(self._turn_count, actor_context_snapshot)
                except Exception as e:
                    logger.debug("Honcho base context fetch failed: %s", e)
                    self._set_base_context_cache("", actor_context_snapshot)
            base_context = self._get_base_context_cache(actor_context_snapshot) or ""

        # Check if background context prefetch has a fresher result
        if self._manager:
            fresh_ctx = self._manager.pop_context_result(
                self._session_key,
                actor_context=actor_context_snapshot,
            )
            if fresh_ctx:
                formatted = self._format_first_turn_context(fresh_ctx)
                if formatted:
                    with self._base_context_lock:
                        self._set_base_context_cache(formatted, actor_context_snapshot)
                    base_context = formatted

        if base_context:
            parts.append(base_context)

        # ----- Layer 2: Dialectic supplement -----
        # On the very first turn, no queue_prefetch() has run yet so the
        # dialectic result is empty.  Run with a bounded timeout so a slow
        # Honcho connection doesn't block the first response indefinitely.
        # On timeout we let the thread keep running and write its result into
        # _prefetch_result under the lock, so the next turn picks it up.
        #
        # Skip if the session-start prewarm already filled _prefetch_result —
        # firing another .chat() would be duplicate work.
        with self._prefetch_lock:
            _prewarm_landed = (
                bool(self._prefetch_result)
                if not self._actor_runtime_enabled
                else self._actor_cache_key(actor_context_snapshot) in self._prefetch_results_by_actor
            )
        _last_dialectic_turn = self._get_last_dialectic_turn(actor_context_snapshot)
        if _prewarm_landed and _last_dialectic_turn == -999:
            self._set_last_dialectic_turn(self._turn_count, actor_context_snapshot)
            _last_dialectic_turn = self._turn_count

        if _last_dialectic_turn == -999 and query:
            _first_turn_timeout = (
                self._config.timeout if self._config and self._config.timeout else 8.0
            )
            _fired_at = self._turn_count

            def _run_first_turn() -> None:
                try:
                    r = self._run_dialectic_depth(
                        query,
                        actor_context=actor_context_snapshot,
                    )
                except Exception as exc:
                    logger.debug("Honcho first-turn dialectic failed: %s", exc)
                    self._increment_empty_streak(actor_context_snapshot)
                    return
                if r and r.strip():
                    self._store_prefetch_result(
                        r,
                        _fired_at,
                        actor_context=actor_context_snapshot,
                    )
                    # Advance cadence only on a non-empty result so the next
                    # turn retries when the call returned nothing.
                    self._set_last_dialectic_turn(_fired_at, actor_context_snapshot)
                    self._set_empty_streak(0, actor_context_snapshot)
                else:
                    self._increment_empty_streak(actor_context_snapshot)

            _started_at = time.monotonic()
            _thread = threading.Thread(
                target=_run_first_turn, daemon=True, name="honcho-prefetch-first"
            )
            self._set_prefetch_thread(_thread, _started_at, actor_context_snapshot)
            _thread.start()
            _thread.join(timeout=_first_turn_timeout)
            if _thread.is_alive():
                logger.debug(
                    "Honcho first-turn dialectic still running after %.1fs — "
                    "will surface on next turn",
                    _first_turn_timeout,
                )

        _thread, _ = self._get_prefetch_thread(actor_context_snapshot)
        if _thread and _thread.is_alive():
            _thread.join(timeout=3.0)
        dialectic_result, fired_at = self._pop_prefetch_result(actor_context_snapshot)

        # Discard stale pending results: if the fire happened more than
        # cadence × multiplier turns ago (e.g. a run of trivial-prompt turns
        # passed without consumption), the content likely no longer tracks
        # the current conversational pivot.
        stale_limit = self._dialectic_cadence * self._STALE_RESULT_MULTIPLIER
        if dialectic_result and fired_at >= 0 and (self._turn_count - fired_at) > stale_limit:
            logger.debug(
                "Honcho pending dialectic discarded as stale: fired_at=%d, "
                "turn=%d, limit=%d", fired_at, self._turn_count, stale_limit,
            )
            dialectic_result = ""

        if dialectic_result and dialectic_result.strip():
            parts.append(dialectic_result)

        if not parts:
            return ""

        result = "\n\n".join(parts)

        # ----- Port #3265: token budget enforcement -----
        result = self._truncate_to_budget(result)

        return result

    def _truncate_to_budget(self, text: str) -> str:
        """Truncate text to fit within context_tokens budget if set."""
        if not self._config or not self._config.context_tokens:
            return text
        budget_chars = self._config.context_tokens * 4  # conservative char estimate
        if len(text) <= budget_chars:
            return text
        # Truncate at word boundary
        truncated = text[:budget_chars]
        last_space = truncated.rfind(" ")
        if last_space > budget_chars * 0.8:
            truncated = truncated[:last_space]
        return truncated + " …"

    def queue_prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        actor_context: dict[str, Any] | None = None,
    ) -> None:
        """Fire background prefetch threads for the upcoming turn.

        B5: Checks cadence independently for dialectic and context refresh.
        Context refresh updates the base layer (representation + card).
        Dialectic fires the LLM reasoning supplement.
        """
        if not self._manager or not self._session_key or not query:
            return

        # B1: tools-only mode — no prefetch
        if self._recall_mode == "tools":
            return

        # Trivial prompts don't warrant either a context refresh or a dialectic call.
        if self._is_trivial_prompt(query):
            return

        actor_context_snapshot = self._turn_actor_context(actor_context)

        # ----- Context refresh (base layer) — independent cadence -----
        last_context_turn = self._get_last_context_turn(actor_context_snapshot)
        if self._context_cadence <= 1 or (self._turn_count - last_context_turn) >= self._context_cadence:
            self._set_last_context_turn(self._turn_count, actor_context_snapshot)
            try:
                self._manager.prefetch_context(
                    self._session_key,
                    query,
                    actor_context=actor_context_snapshot,
                )
            except Exception as e:
                logger.debug("Honcho context prefetch failed: %s", e)

        # ----- Dialectic prefetch (supplement layer) -----
        # Thread-alive guard with stale-thread recovery: a hung Honcho call
        # older than timeout × multiplier is treated as dead so it can't
        # block subsequent fires.
        if self._thread_is_live(actor_context_snapshot):
            logger.debug("Honcho dialectic prefetch skipped: prior thread still running")
            return

        # Cadence gate, widened by the empty-streak backoff so a persistently
        # silent backend doesn't retry every turn forever.
        effective = self._effective_cadence(actor_context_snapshot)
        last_dialectic_turn = self._get_last_dialectic_turn(actor_context_snapshot)
        if (self._turn_count - last_dialectic_turn) < effective:
            logger.debug(
                "Honcho dialectic prefetch skipped: effective cadence %d "
                "(base %d, empty streak %d), turns since last: %d",
                effective, self._dialectic_cadence, self._get_empty_streak(actor_context_snapshot),
                self._turn_count - last_dialectic_turn,
            )
            return

        # Cadence advances only on a non-empty result so empty returns
        # (transient API error, sparse representation) retry next turn.
        _fired_at = self._turn_count

        def _run():
            try:
                result = self._run_dialectic_depth(
                    query,
                    actor_context=actor_context_snapshot,
                )
            except Exception as e:
                logger.debug("Honcho prefetch failed: %s", e)
                self._increment_empty_streak(actor_context_snapshot)
                return
            if result and result.strip():
                self._store_prefetch_result(
                    result,
                    _fired_at,
                    actor_context=actor_context_snapshot,
                )
                self._set_last_dialectic_turn(_fired_at, actor_context_snapshot)
                self._set_empty_streak(0, actor_context_snapshot)
            else:
                self._increment_empty_streak(actor_context_snapshot)

        _started_at = time.monotonic()
        _thread = threading.Thread(
            target=_run, daemon=True, name="honcho-prefetch"
        )
        self._set_prefetch_thread(_thread, _started_at, actor_context_snapshot)
        _thread.start()

    # ----- Dialectic depth: multi-pass .chat() with cold/warm prompts -----

    # Proportional reasoning levels per depth/pass when dialecticDepthLevels
    # is not configured. The base level is dialecticReasoningLevel.
    # Index: (depth, pass) → level relative to base.
    _PROPORTIONAL_LEVELS: dict[tuple[int, int], str] = {
        # depth 1: single pass at base level
        (1, 0): "base",
        # depth 2: pass 0 lighter, pass 1 at base
        (2, 0): "minimal",
        (2, 1): "base",
        # depth 3: pass 0 lighter, pass 1 at base, pass 2 one above minimal
        (3, 0): "minimal",
        (3, 1): "base",
        (3, 2): "low",
    }

    _LEVEL_ORDER = ("minimal", "low", "medium", "high", "max")

    # Char-count thresholds for the query-length reasoning heuristic.
    _HEURISTIC_LENGTH_MEDIUM = 120
    _HEURISTIC_LENGTH_HIGH = 400

    # Liveness constants. A thread older than timeout × multiplier is treated
    # as dead so a hung Honcho call can't block future retries indefinitely.
    _STALE_THREAD_MULTIPLIER = 2.0
    # Pending result whose fire-turn is older than cadence × multiplier is
    # discarded on read so we don't inject context for a stale conversational
    # pivot after a gap of trivial-prompt turns.
    _STALE_RESULT_MULTIPLIER = 2
    # Cap on the empty-streak backoff so a persistently silent backend
    # eventually settles on a ceiling instead of unbounded widening.
    _BACKOFF_MAX = 8

    def _thread_is_live(self, actor_context: dict[str, Any] | None = None) -> bool:
        """Thread-alive guard that treats threads older than the stale
        threshold as dead, so a hung Honcho request can't block new fires."""
        thread, started_at = self._get_prefetch_thread(actor_context)
        if not thread or not thread.is_alive():
            return False
        timeout = (self._config.timeout if self._config and self._config.timeout else 8.0)
        age = time.monotonic() - started_at
        if age > timeout * self._STALE_THREAD_MULTIPLIER:
            logger.debug(
                "Honcho prefetch thread age %.1fs exceeds stale threshold "
                "%.1fs — treating as dead", age, timeout * self._STALE_THREAD_MULTIPLIER,
            )
            return False
        return True

    def _effective_cadence(self, actor_context: dict[str, Any] | None = None) -> int:
        """Cadence plus empty-streak backoff, capped at _BACKOFF_MAX × base."""
        empty_streak = self._get_empty_streak(actor_context)
        if empty_streak <= 0:
            return self._dialectic_cadence
        widened = self._dialectic_cadence + empty_streak
        ceiling = self._dialectic_cadence * self._BACKOFF_MAX
        return min(widened, ceiling)

    def liveness_snapshot(self) -> dict:
        """In-process snapshot of dialectic liveness state for diagnostics.

        Returns current turn, last successful dialectic turn, pending-result
        fire turn, empty streak, effective cadence, and thread status.
        """
        thread_age = None
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            thread_age = time.monotonic() - self._prefetch_thread_started_at
        return {
            "turn_count": self._turn_count,
            "last_dialectic_turn": self._last_dialectic_turn,
            "pending_result_fired_at": self._prefetch_result_fired_at,
            "empty_streak": self._dialectic_empty_streak,
            "effective_cadence": self._effective_cadence(),
            "thread_alive": thread_age is not None,
            "thread_age_seconds": thread_age,
        }

    def _apply_reasoning_heuristic(self, base: str, query: str) -> str:
        """Scale `base` up by query length, clamped at reasoning_level_cap.

        Char-count heuristic: +1 at >=120 chars, +2 at >=400.
        """
        if not self._reasoning_heuristic or not query:
            return base
        if base not in self._LEVEL_ORDER:
            return base
        n = len(query)
        if n < self._HEURISTIC_LENGTH_MEDIUM:
            bump = 0
        elif n < self._HEURISTIC_LENGTH_HIGH:
            bump = 1
        else:
            bump = 2
        base_idx = self._LEVEL_ORDER.index(base)
        cap_idx = self._LEVEL_ORDER.index(self._reasoning_level_cap)
        return self._LEVEL_ORDER[min(base_idx + bump, cap_idx)]

    def _resolve_pass_level(self, pass_idx: int, query: str = "") -> str:
        """Resolve reasoning level for a given pass index.

        Precedence:
          1. dialecticDepthLevels (explicit per-pass) — wins absolutely
          2. _PROPORTIONAL_LEVELS table (depth>1 lighter-early passes)
          3. Base level = dialecticReasoningLevel, optionally scaled by the
             reasoning heuristic when the mapping falls through to 'base'
        """
        if self._dialectic_depth_levels and pass_idx < len(self._dialectic_depth_levels):
            return self._dialectic_depth_levels[pass_idx]

        base = (self._config.dialectic_reasoning_level if self._config else "low")
        mapping = self._PROPORTIONAL_LEVELS.get((self._dialectic_depth, pass_idx))
        if mapping is None or mapping == "base":
            return self._apply_reasoning_heuristic(base, query)
        return mapping

    def _build_dialectic_prompt(self, pass_idx: int, prior_results: list[str], is_cold: bool) -> str:
        """Build the prompt for a given dialectic pass.

        Pass 0: cold start (general user query) or warm (session-scoped).
        Pass 1: self-audit / targeted synthesis against gaps from pass 0.
        Pass 2: reconciliation / contradiction check across prior passes.
        """
        if pass_idx == 0:
            if is_cold:
                return (
                    "Who is this person? What are their preferences, goals, "
                    "and working style? Focus on facts that would help an AI "
                    "assistant be immediately useful."
                )
            return (
                "Given what's been discussed in this session so far, what "
                "context about this user is most relevant to the current "
                "conversation? Prioritize active context over biographical facts."
            )
        elif pass_idx == 1:
            prior = prior_results[-1] if prior_results else ""
            return (
                f"Given this initial assessment:\n\n{prior}\n\n"
                "What gaps remain in your understanding that would help "
                "going forward? Synthesize what you actually know about "
                "the user's current state and immediate needs, grounded "
                "in evidence from recent sessions."
            )
        else:
            # pass 2: reconciliation
            return (
                f"Prior passes produced:\n\n"
                f"Pass 1:\n{prior_results[0] if len(prior_results) > 0 else '(empty)'}\n\n"
                f"Pass 2:\n{prior_results[1] if len(prior_results) > 1 else '(empty)'}\n\n"
                "Do these assessments cohere? Reconcile any contradictions "
                "and produce a final, concise synthesis of what matters most "
                "for the current conversation."
            )

    @staticmethod
    def _signal_sufficient(result: str) -> bool:
        """Check if a dialectic pass returned enough signal to skip further passes.

        Heuristic: a response longer than 100 chars with some structure
        (section headers, bullets, or an ordered list) is considered sufficient.
        """
        if not result or len(result.strip()) < 100:
            return False
        # Structured output with sections/bullets is strong signal
        if "\n" in result and (
            "##" in result
            or "•" in result
            or re.search(r"^[*-] ", result, re.MULTILINE)
            or re.search(r"^\s*\d+\. ", result, re.MULTILINE)
        ):
            return True
        # Long enough even without structure
        return len(result.strip()) > 300

    def _run_dialectic_depth(
        self,
        query: str,
        *,
        actor_context: dict[str, Any] | None = None,
    ) -> str:
        """Execute up to dialecticDepth .chat() calls with conditional bail-out.

        Cold start (no base context): general user-oriented query.
        Warm session (base context exists): session-scoped query.
        Each pass is conditional — bails early if prior pass returned strong signal.
        Returns the best (usually last) result.
        """
        if not self._manager or not self._session_key:
            return ""

        actor_context_snapshot = self._turn_actor_context(actor_context)
        is_cold = not self._get_base_context_cache(actor_context_snapshot)
        results: list[str] = []

        for i in range(self._dialectic_depth):
            if i == 0:
                prompt = self._build_dialectic_prompt(0, results, is_cold)
            else:
                # Skip further passes if prior pass delivered strong signal
                if results and self._signal_sufficient(results[-1]):
                    logger.debug("Honcho dialectic depth %d: pass %d skipped, prior signal sufficient",
                                 self._dialectic_depth, i)
                    break
                prompt = self._build_dialectic_prompt(i, results, is_cold)

            level = self._resolve_pass_level(i, query=query)
            logger.debug("Honcho dialectic depth %d: pass %d, level=%s, cold=%s",
                         self._dialectic_depth, i, level, is_cold)

            result = self._manager.dialectic_query(
                self._session_key, prompt,
                reasoning_level=level,
                peer="user",
                actor_context=actor_context_snapshot,
            )
            results.append(result or "")

        # Return the last non-empty result (deepest pass that ran)
        for r in reversed(results):
            if r and r.strip():
                return r
        return ""

    # Prompts that carry no semantic signal — trivial acknowledgements, slash
    # commands, empty input. Skipping injection here saves tokens and prevents
    # stale user-model context from derailing one-word replies.
    _TRIVIAL_PROMPT_RE = re.compile(
        r'^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|'
        r'continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)$',
        re.IGNORECASE,
    )

    @classmethod
    def _is_trivial_prompt(cls, text: str) -> bool:
        """Return True if the prompt is too trivial to warrant context injection."""
        if not text:
            return True
        stripped = text.strip()
        if not stripped:
            return True
        if stripped.startswith("/"):
            return True
        if cls._TRIVIAL_PROMPT_RE.match(stripped):
            return True
        return False

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Track turn count for cadence and injection_frequency logic."""
        self._turn_count = turn_number
        if self._actor_runtime_enabled:
            self.set_actor_context(kwargs.get("actor_context") or {})

    @staticmethod
    def _chunk_message(content: str, limit: int) -> list[str]:
        """Split content into chunks that fit within the Honcho message limit.

        Splits at paragraph boundaries when possible, falling back to
        sentence boundaries, then word boundaries. Each continuation
        chunk is prefixed with "[continued] " so Honcho's representation
        engine can reconstruct the full message.
        """
        if len(content) <= limit:
            return [content]

        prefix = "[continued] "
        prefix_len = len(prefix)
        chunks = []
        remaining = content
        first = True
        while remaining:
            effective = limit if first else limit - prefix_len
            if len(remaining) <= effective:
                chunks.append(remaining if first else prefix + remaining)
                break

            segment = remaining[:effective]

            # Try paragraph break, then sentence, then word
            cut = segment.rfind("\n\n")
            if cut < effective * 0.3:
                cut = segment.rfind(". ")
                if cut >= 0:
                    cut += 2  # include the period and space
            if cut < effective * 0.3:
                cut = segment.rfind(" ")
            if cut < effective * 0.3:
                cut = effective  # hard cut

            chunk = remaining[:cut].rstrip()
            remaining = remaining[cut:].lstrip()
            if not first:
                chunk = prefix + chunk
            chunks.append(chunk)
            first = False

        return chunks

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        actor_context: dict[str, Any] | None = None,
    ) -> None:
        """Record the conversation turn in Honcho (non-blocking).

        Messages exceeding the Honcho API limit (default 25k chars) are
        split into multiple messages with continuation markers.
        """
        if not self._manager or not self._session_key:
            return

        msg_limit = self._config.message_max_chars if self._config else 25000
        actor_context_snapshot = self._turn_actor_context(actor_context)
        user_peer_id = None
        if self._actor_runtime_enabled:
            try:
                user_peer_id = self._manager.current_actor_peer_id(
                    actor_context=actor_context_snapshot,
                )
            except Exception:
                user_peer_id = actor_context_snapshot.get("peer_id")

        def _sync():
            try:
                session = self._manager.get_or_create(
                    self._session_key,
                    actor_context=actor_context_snapshot,
                )
                turn_user_peer_id = user_peer_id
                if self._actor_runtime_enabled:
                    if not turn_user_peer_id:
                        turn_user_peer_id = self._manager.current_actor_peer_id(
                            session,
                            actor_context=actor_context_snapshot,
                        )
                    self._manager._ensure_session_peer(session, turn_user_peer_id)
                    self._manager._ensure_session_peer(session, session.assistant_peer_id)
                for chunk in self._chunk_message(user_content, msg_limit):
                    session.add_message("user", chunk, peer_id=turn_user_peer_id)
                for chunk in self._chunk_message(assistant_content, msg_limit):
                    session.add_message("assistant", chunk, peer_id=session.assistant_peer_id if self._actor_runtime_enabled else None)
                self._manager._flush_session(session)
            except Exception as e:
                logger.debug("Honcho sync_turn failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="honcho-sync"
        )
        self._sync_thread.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        *,
        actor_context: dict[str, Any] | None = None,
    ) -> None:
        """Mirror built-in user profile writes as Honcho conclusions."""
        if action != "add" or target != "user" or not content:
            return
        if not self._manager or not self._session_key:
            return
        actor_context_snapshot = self._turn_actor_context(actor_context)

        def _write():
            try:
                self._manager.create_conclusion(
                    self._session_key,
                    content,
                    actor_context=actor_context_snapshot,
                )
            except Exception as e:
                logger.debug("Honcho memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="honcho-memwrite")
        t.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush all pending messages to Honcho on session end."""
        if not self._manager:
            return
        # Wait for pending sync
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)
        try:
            self._manager.flush_all()
        except Exception as e:
            logger.debug("Honcho session-end flush failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas, respecting recall_mode.

        B1: context-only mode hides all tools.
        """
        if self._recall_mode == "context":
            return []
        return list(ALL_TOOL_SCHEMAS)

    def _authorize_tool_peer(
        self,
        peer: Any,
        *,
        write: bool = False,
        actor_context: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        """Authorize a Honcho tool peer target under actor runtime.

        In multiplayer sessions, peer scoping is a correctness boundary. A
        non-owner may access the current speaker alias and read the AI alias,
        but explicit cross-peer targeting requires owner authority.
        """
        peer_value = str(peer or "user").strip() or "user"
        if not self._actor_runtime_enabled:
            return peer_value, None

        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", peer_value).strip("-").lower()
        is_user_alias = normalized in {"", "user"}
        is_ai_alias = normalized == "ai"
        authority = str((actor_context or {}).get("authority") or "").lower()
        privileged = authority == "owner"

        if is_user_alias:
            return "user", None
        if is_ai_alias and not write:
            return "ai", None
        if privileged:
            return peer_value, None

        return "", "Honcho peer access denied: explicit peer targeting requires owner authority."

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        """Handle a Honcho tool call, with lazy session init for tools-only mode."""

        # Port #1957: ensure session is initialized for tools-only mode
        if not self._session_initialized:
            if not self._ensure_session():
                return tool_error("Honcho session could not be initialized.")

        if not self._manager or not self._session_key:
            return tool_error("Honcho is not active for this session.")

        try:
            actor_context_snapshot = self._turn_actor_context(kwargs.get("actor_context"))
            if tool_name == "honcho_profile":
                card_update = args.get("card")
                peer, denial = self._authorize_tool_peer(
                    args.get("peer", "user"),
                    write=bool(card_update),
                    actor_context=actor_context_snapshot,
                )
                if denial:
                    return tool_error(denial)
                if card_update:
                    result = self._manager.set_peer_card(
                        self._session_key,
                        card_update,
                        peer=peer,
                        **self._actor_kwargs(actor_context_snapshot),
                    )
                    if result is None:
                        return tool_error("Failed to update peer card.")
                    return json.dumps({"result": f"Peer card updated ({len(result)} facts).", "card": result})
                card = self._manager.get_peer_card(
                    self._session_key,
                    peer=peer,
                    **self._actor_kwargs(actor_context_snapshot),
                )
                if not card:
                    return json.dumps({"result": "No profile facts available yet."})
                return json.dumps({"result": card})

            elif tool_name == "honcho_search":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                max_tokens = min(int(args.get("max_tokens", 800)), 2000)
                peer, denial = self._authorize_tool_peer(
                    args.get("peer", "user"),
                    actor_context=actor_context_snapshot,
                )
                if denial:
                    return tool_error(denial)
                result = self._manager.search_context(
                    self._session_key,
                    query,
                    max_tokens=max_tokens,
                    peer=peer,
                    **self._actor_kwargs(actor_context_snapshot),
                )
                if not result:
                    return json.dumps({"result": "No relevant context found."})
                return json.dumps({"result": result})

            elif tool_name == "honcho_reasoning":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                peer, denial = self._authorize_tool_peer(
                    args.get("peer", "user"),
                    actor_context=actor_context_snapshot,
                )
                if denial:
                    return tool_error(denial)
                reasoning_level = args.get("reasoning_level")
                result = self._manager.dialectic_query(
                    self._session_key, query,
                    reasoning_level=reasoning_level,
                    peer=peer,
                    **self._actor_kwargs(actor_context_snapshot),
                )
                # Update cadence tracker so auto-injection respects the gap after an explicit call
                self._set_last_dialectic_turn(self._turn_count, actor_context_snapshot)
                return json.dumps({"result": result or "No result from Honcho."})

            elif tool_name == "honcho_context":
                peer, denial = self._authorize_tool_peer(
                    args.get("peer", "user"),
                    actor_context=actor_context_snapshot,
                )
                if denial:
                    return tool_error(denial)
                ctx = self._manager.get_session_context(
                    self._session_key,
                    peer=peer,
                    **self._actor_kwargs(actor_context_snapshot),
                )
                if not ctx:
                    return json.dumps({"result": "No context available yet."})
                parts = []
                if ctx.get("summary"):
                    parts.append(f"## Summary\n{ctx['summary']}")
                if ctx.get("representation"):
                    parts.append(f"## Representation\n{ctx['representation']}")
                if ctx.get("card"):
                    parts.append(f"## Card\n{ctx['card']}")
                if ctx.get("recent_messages"):
                    msgs = ctx["recent_messages"]
                    msg_str = "\n".join(
                        f"  [{m['role']}] {m['content'][:200]}"
                        for m in msgs[-5:]  # last 5 for brevity
                    )
                    parts.append(f"## Recent messages\n{msg_str}")
                return json.dumps({"result": "\n\n".join(parts) or "No context available."})

            elif tool_name == "honcho_conclude":
                delete_id = (args.get("delete_id") or "").strip()
                conclusion = args.get("conclusion", "").strip()
                peer, denial = self._authorize_tool_peer(
                    args.get("peer", "user"),
                    write=True,
                    actor_context=actor_context_snapshot,
                )
                if denial:
                    return tool_error(denial)

                has_delete_id = bool(delete_id)
                has_conclusion = bool(conclusion)
                if has_delete_id == has_conclusion:
                    return tool_error("Exactly one of conclusion or delete_id must be provided.")

                if has_delete_id:
                    ok = self._manager.delete_conclusion(
                        self._session_key,
                        delete_id,
                        peer=peer,
                        **self._actor_kwargs(actor_context_snapshot),
                    )
                    if ok:
                        return json.dumps({"result": f"Conclusion {delete_id} deleted."})
                    return tool_error(f"Failed to delete conclusion {delete_id}.")
                ok = self._manager.create_conclusion(
                    self._session_key,
                    conclusion,
                    peer=peer,
                    **self._actor_kwargs(actor_context_snapshot),
                )
                if ok:
                    return json.dumps({"result": f"Conclusion saved for {peer}: {conclusion}"})
                return tool_error("Failed to save conclusion.")

            return tool_error(f"Unknown tool: {tool_name}")

        except Exception as e:
            logger.error("Honcho tool %s failed: %s", tool_name, e)
            return tool_error(f"Honcho {tool_name} failed: {e}")

    def shutdown(self) -> None:
        threads = [self._prefetch_thread, self._sync_thread]
        threads.extend(self._prefetch_threads_by_actor.values())
        for t in threads:
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Flush any remaining messages
        if self._manager:
            try:
                self._manager.flush_all()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register Honcho as a memory provider plugin."""
    ctx.register_memory_provider(HonchoMemoryProvider())
