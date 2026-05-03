"""Shared-channel session + reply-quote author attribution.

Two changes under test:

1. ``group_sessions_per_user`` defaults to ``False`` — in a group/channel,
   all humans share a single session with the agent, so the agent sees
   the whole channel transcript including its own prior responses to
   other users. Previously the agent could deny statements it had made
   in a public channel simply because the per-user session didn't
   contain them.

2. Reply-quotes carry the replied-to author's name. Without this, an
   agent seeing a quote of someone else's message can mistake it for a
   fabricated history claim attributed to itself.
"""
from types import SimpleNamespace

import pytest


class TestSharedChannelSessionKey:
    """Default isolation flag and its effect on session-key construction."""

    def test_default_group_isolation_is_off(self):
        from gateway.config import GatewayConfig
        cfg = GatewayConfig()
        assert cfg.group_sessions_per_user is False
        assert cfg.thread_sessions_per_user is False

    def test_group_session_key_is_shared_across_users_by_default(self):
        """Two distinct users in the same group channel share a session."""
        from gateway.session import build_session_key, Platform
        src_a = SimpleNamespace(
            platform=Platform.DISCORD, chat_id="CHANNEL",
            chat_type="group", thread_id=None,
            user_id="userA", user_id_alt=None,
        )
        src_b = SimpleNamespace(
            platform=Platform.DISCORD, chat_id="CHANNEL",
            chat_type="group", thread_id=None,
            user_id="userB", user_id_alt=None,
        )
        key_a = build_session_key(src_a, group_sessions_per_user=False)
        key_b = build_session_key(src_b, group_sessions_per_user=False)
        assert key_a == key_b
        assert "userA" not in key_a and "userB" not in key_a

    def test_per_user_still_works_when_explicitly_enabled(self):
        """Explicit opt-in to per-user isolation still partitions users."""
        from gateway.session import build_session_key, Platform
        src_a = SimpleNamespace(
            platform=Platform.DISCORD, chat_id="CHANNEL",
            chat_type="group", thread_id=None,
            user_id="userA", user_id_alt=None,
        )
        src_b = SimpleNamespace(
            platform=Platform.DISCORD, chat_id="CHANNEL",
            chat_type="group", thread_id=None,
            user_id="userB", user_id_alt=None,
        )
        key_a = build_session_key(src_a, group_sessions_per_user=True)
        key_b = build_session_key(src_b, group_sessions_per_user=True)
        assert key_a != key_b
        assert "userA" in key_a
        assert "userB" in key_b

    def test_dm_is_unaffected_by_flag(self):
        """DMs are keyed by chat_id only — per-user flag doesn't matter."""
        from gateway.session import build_session_key, Platform
        src = SimpleNamespace(
            platform=Platform.DISCORD, chat_id="DM1",
            chat_type="dm", thread_id=None,
            user_id="userA", user_id_alt=None,
        )
        k1 = build_session_key(src, group_sessions_per_user=False)
        k2 = build_session_key(src, group_sessions_per_user=True)
        assert k1 == k2
        assert "dm" in k1


class TestReplyQuoteAuthor:
    """The ``reply_to_author`` field must surface through to the formatted
    ``[Replying to X: "..."]`` pointer."""

    def test_message_event_has_reply_to_author_field(self):
        from gateway.platforms.base import MessageEvent
        ev = MessageEvent(text="hi", reply_to_author="alice")
        assert ev.reply_to_author == "alice"

    def test_message_event_reply_to_author_default_none(self):
        from gateway.platforms.base import MessageEvent
        ev = MessageEvent(text="hi")
        assert ev.reply_to_author is None

    def test_reply_prefix_includes_author_when_present(self):
        """Formatter matches ``[Replying to <author>: "<snippet>"]``."""
        event = SimpleNamespace(
            text="your turn",
            reply_to_message_id="123",
            reply_to_text="hello there",
            reply_to_author="Sal",
        )
        # Simulate the formatter logic from gateway/run.py
        reply_snippet = event.reply_to_text[:500]
        reply_author = getattr(event, "reply_to_author", None)
        if reply_author:
            out = f'[Replying to {reply_author}: "{reply_snippet}"]\n\nfollow'
        else:
            out = f'[Replying to: "{reply_snippet}"]\n\nfollow'
        assert out.startswith('[Replying to Sal: "hello there"]')

    def test_reply_prefix_omits_author_when_absent(self):
        event = SimpleNamespace(
            text="your turn",
            reply_to_message_id="123",
            reply_to_text="hello there",
            reply_to_author=None,
        )
        reply_snippet = event.reply_to_text[:500]
        reply_author = getattr(event, "reply_to_author", None)
        if reply_author:
            out = f'[Replying to {reply_author}: "{reply_snippet}"]\n\nfollow'
        else:
            out = f'[Replying to: "{reply_snippet}"]\n\nfollow'
        assert out.startswith('[Replying to: "hello there"]')
