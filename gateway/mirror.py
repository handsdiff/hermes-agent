"""
Session mirroring for cross-platform message delivery.

When a message is sent to a platform (via send_message or cron delivery),
this module appends a "delivery-mirror" record to the target session's
transcript so the receiving-side agent has context about what was sent.

Standalone -- works from CLI, cron, and gateway contexts without needing
the full SessionStore machinery.

When no session exists for the destination channel (common for cron-
originated outbounds to channels the agent has never received a message
in), a minimal session record is created so the mirrored outbound
becomes the starting point of the channel's shared transcript. Without
this, an agent that posts to a channel from a cron would deny ever
having made the post when asked about it there later.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

_SESSIONS_DIR = get_hermes_home() / "sessions"
_SESSIONS_INDEX = _SESSIONS_DIR / "sessions.json"


def mirror_to_session(
    platform: str,
    chat_id: str,
    message_text: str,
    source_label: str = "cli",
    thread_id: Optional[str] = None,
) -> bool:
    """
    Append a delivery-mirror message to the target session's transcript.

    Finds (or creates) the gateway session that matches the given
    platform + chat_id, then writes a mirror entry to both the JSONL
    transcript and SQLite DB.

    Returns True if mirrored successfully, False on error.
    All errors are caught — this is never fatal.
    """
    try:
        session_id = _find_session_id(platform, str(chat_id), thread_id=thread_id)
        if not session_id:
            session_id = _create_channel_session(
                platform, str(chat_id), thread_id=thread_id,
            )
            if not session_id:
                logger.debug(
                    "Mirror: could not find or create session for %s:%s:%s",
                    platform, chat_id, thread_id,
                )
                return False

        mirror_msg = {
            "role": "assistant",
            "content": message_text,
            "timestamp": datetime.now().isoformat(),
            "mirror": True,
            "mirror_source": source_label,
        }

        _append_to_jsonl(session_id, mirror_msg)
        _append_to_sqlite(session_id, mirror_msg)

        logger.debug("Mirror: wrote to session %s (from %s)", session_id, source_label)
        return True

    except Exception as e:
        logger.debug("Mirror failed for %s:%s:%s: %s", platform, chat_id, thread_id, e)
        return False


def _create_channel_session(
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> Optional[str]:
    """Create a minimal session record for a channel outbound when none exists.

    Writes a sessions.json entry + a row in the SQLite sessions table so
    subsequent mirror calls and subsequent inbound messages find and
    extend the same session. Returns the new session_id on success.

    The session key mirrors ``build_session_key``'s shared-channel shape
    (``agent:main:<platform>:group:<chat_id>[:<thread_id>]``) so an
    incoming @mention from any user in the same channel resumes this
    session instead of starting a fresh one.
    """
    try:
        now = datetime.now(timezone.utc)
        session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        # Build a shared-channel session key.  DMs keep their chat_id-scoped
        # shape; groups drop the per-user suffix so every participant lands
        # on the same session (same rule as build_session_key with
        # ``group_sessions_per_user=False``).
        parts = ["agent:main", platform.lower(), "group", str(chat_id)]
        if thread_id:
            parts.append(str(thread_id))
        session_key = ":".join(parts)

        # Write sessions.json entry
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        index = {}
        if _SESSIONS_INDEX.exists():
            try:
                with open(_SESSIONS_INDEX, encoding="utf-8") as f:
                    index = json.load(f)
            except Exception:
                index = {}

        origin = {
            "platform": platform.lower(),
            "chat_id": str(chat_id),
            "chat_type": "group",
        }
        if thread_id:
            origin["thread_id"] = str(thread_id)

        index[session_key] = {
            "session_key": session_key,
            "session_id": session_id,
            "origin": origin,
            "platform": platform.lower(),
            "chat_type": "group",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        tmp = _SESSIONS_INDEX.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, default=str)
        tmp.replace(_SESSIONS_INDEX)

        # Create the SQLite sessions row so append_message has a FK target
        try:
            from hermes_state import SessionDB
            db = SessionDB()
            try:
                db.create_session(
                    session_id=session_id,
                    source=platform.lower(),
                    user_id=None,
                )
            finally:
                db.close()
        except Exception as e:
            logger.debug("Mirror create-session SQLite write failed: %s", e)

        logger.debug(
            "Mirror: created session %s for %s:%s:%s",
            session_id, platform, chat_id, thread_id,
        )
        return session_id

    except Exception as e:
        logger.debug(
            "Mirror session-create failed for %s:%s:%s: %s",
            platform, chat_id, thread_id, e,
        )
        return None


def _find_session_id(platform: str, chat_id: str, thread_id: Optional[str] = None) -> Optional[str]:
    """
    Find the active session_id for a platform + chat_id pair.

    Scans sessions.json entries and matches where origin.chat_id == chat_id
    on the right platform.  DM session keys don't embed the chat_id
    (e.g. "agent:main:telegram:dm"), so we check the origin dict.
    """
    if not _SESSIONS_INDEX.exists():
        return None

    try:
        with open(_SESSIONS_INDEX, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    platform_lower = platform.lower()
    best_match = None
    best_updated = ""

    for _key, entry in data.items():
        origin = entry.get("origin") or {}
        entry_platform = (origin.get("platform") or entry.get("platform", "")).lower()

        if entry_platform != platform_lower:
            continue

        origin_chat_id = str(origin.get("chat_id", ""))
        if origin_chat_id == str(chat_id):
            origin_thread_id = origin.get("thread_id")
            if thread_id is not None and str(origin_thread_id or "") != str(thread_id):
                continue
            updated = entry.get("updated_at", "")
            if updated > best_updated:
                best_updated = updated
                best_match = entry.get("session_id")

    return best_match


def _append_to_jsonl(session_id: str, message: dict) -> None:
    """Append a message to the JSONL transcript file."""
    transcript_path = _SESSIONS_DIR / f"{session_id}.jsonl"
    try:
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("Mirror JSONL write failed: %s", e)


def _append_to_sqlite(session_id: str, message: dict) -> None:
    """Append a message to the SQLite session database."""
    db = None
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        db.append_message(
            session_id=session_id,
            role=message.get("role", "assistant"),
            content=message.get("content"),
        )
    except Exception as e:
        logger.debug("Mirror SQLite write failed: %s", e)
    finally:
        if db is not None:
            db.close()
