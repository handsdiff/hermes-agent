import json

from gateway.agent_actor import (
    build_honcho_actor_context,
    build_state_packet,
    detect_public_broadcast_stop_directive,
    evaluate_send_message_policy,
    infer_platform_authority,
    maybe_record_directive_from_inbound,
    owner_user_ids_for_platform,
    record_inbound_event,
)
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB


def test_detects_public_broadcast_stop_directive():
    directive = detect_public_broadcast_stop_directive("Is this a cron? Turn this off")

    assert directive is not None
    assert directive["behavior"] == "suppress"


def test_state_packet_is_sender_scoped(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="149",
        chat_type="group",
        user_id="141",
        user_name="hands",
    )
    event_id, person_id = record_inbound_event(
        db,
        source=source,
        session_id="sid",
        session_key="agent:main:discord:group:149:141",
        text="hello",
        authority="trusted",
    )

    packet = build_state_packet(
        db,
        source=source,
        session_id="sid",
        session_key="agent:main:discord:group:149:141",
        inbound_event_id=event_id,
        person_id=person_id,
        authority="trusted",
    )

    assert "person_id: discord:141" in packet
    assert "authority: trusted" in packet
    assert "session_key: agent:main:discord:group:149:141" in packet
    db.close()


def test_owner_authority_uses_generated_soul_owner_block(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "SOUL.md").write_text(
        "## Your owner\n"
        "- Discord: `@handsdiff` (user_id `1417636184355766305`)\n"
        "\n## Peer roster\n"
        "- Someone else user_id `999999999999999999`\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    monkeypatch.delenv("DISCORD_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("DISCORD_OWNER_USER_IDS", raising=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1495468809216327702",
        chat_type="group",
        user_id="1417636184355766305",
        user_name="hands",
    )

    assert owner_user_ids_for_platform("discord") == {"1417636184355766305"}
    assert infer_platform_authority(source) == "owner"


def test_honcho_actor_context_scopes_group_senders_separately():
    alice = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="alice-1",
        user_name="Alice",
    )
    bob = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="bob-2",
        user_name="Bob",
    )

    alice_ctx = build_honcho_actor_context(alice, authority="user", agent_id="wait4test")
    bob_ctx = build_honcho_actor_context(bob, authority="user", agent_id="wait4test")

    assert alice_ctx["peer_id"] == "human_discord_alice-1"
    assert bob_ctx["peer_id"] == "human_discord_bob-2"
    assert alice_ctx["peer_id"] != bob_ctx["peer_id"]
    assert alice_ctx["agent_peer_id"] == "agent_wait4test"


def test_honcho_actor_context_prefers_stable_alt_user_id():
    source = SessionSource(
        platform=Platform.SIGNAL,
        chat_id="group",
        chat_type="group",
        user_id="+15551234567",
        user_id_alt="uuid:abc-123",
        user_name="Alice",
    )

    ctx = build_honcho_actor_context(source, authority="user", agent_id="wait4test")

    assert ctx["peer_id"] == "human_signal_uuid_abc-123"
    assert ctx["platform_user_id"] == "uuid:abc-123"
    assert ctx["platform_primary_user_id"] == "+15551234567"
    assert ctx["platform_alt_user_id"] == "uuid:abc-123"


def test_honcho_actor_context_uses_owner_peer_for_owner():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="141",
        user_name="hands",
    )

    ctx = build_honcho_actor_context(source, authority="owner", agent_id="wait4test")

    assert ctx["peer_id"] == "owner"
    assert ctx["peer_kind"] == "owner"
    assert ctx["authority"] == "owner"


def test_honcho_actor_context_distinguishes_hub_agents():
    source = SessionSource(
        platform=Platform.HUB,
        chat_id="hub:speculator",
        chat_type="dm",
        user_id="speculator",
        user_name="speculator",
    )

    ctx = build_honcho_actor_context(source, authority="system", agent_id="wait4test")

    assert ctx["peer_id"] == "hub_agent_speculator"
    assert ctx["peer_kind"] == "hub_agent"


def test_owner_state_packet_includes_recent_cross_session_events(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    group_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="141",
        user_name="hands",
    )
    record_inbound_event(
        db,
        source=group_source,
        session_id="group-sid",
        session_key="agent:main:discord:group:general:141",
        text="hey from general",
        authority="owner",
    )
    dm_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm",
        chat_type="dm",
        user_id="141",
        user_name="hands",
    )
    dm_event_id, person_id = record_inbound_event(
        db,
        source=dm_source,
        session_id="dm-sid",
        session_key="agent:main:discord:dm:dm",
        text="do you see general?",
        authority="owner",
    )

    packet = build_state_packet(
        db,
        source=dm_source,
        session_id="dm-sid",
        session_key="agent:main:discord:dm:dm",
        inbound_event_id=dm_event_id,
        person_id=person_id,
        authority="owner",
    )

    assert "authority: owner" in packet
    assert "Recent Runtime Events" in packet
    assert "hey from general" in packet
    db.close()


def test_trusted_state_packet_does_not_include_unrelated_private_events(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    private_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="private-dm",
        chat_type="dm",
        user_id="owner-1",
        user_name="Owner",
    )
    record_inbound_event(
        db,
        source=private_source,
        session_id="private-sid",
        session_key="agent:main:discord:dm:private-dm",
        text="private owner detail",
        authority="owner",
    )
    group_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="trusted-1",
        user_name="Trusted",
    )
    event_id, person_id = record_inbound_event(
        db,
        source=group_source,
        session_id="group-sid",
        session_key="agent:main:discord:group:general",
        text="what happened recently?",
        authority="trusted",
    )

    packet = build_state_packet(
        db,
        source=group_source,
        session_id="group-sid",
        session_key="agent:main:discord:group:general",
        inbound_event_id=event_id,
        person_id=person_id,
        authority="trusted",
    )

    assert "what happened recently?" in packet
    assert "private owner detail" not in packet
    db.close()


def test_directive_blocks_public_cross_session_send(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="141",
        user_name="hands",
    )
    event_id, person_id = record_inbound_event(
        db,
        source=source,
        session_id="sid",
        session_key="agent:main:discord:group:general:141",
        text="Is this a cron? Turn this off",
        authority="trusted",
    )
    maybe_record_directive_from_inbound(
        db,
        source=source,
        session_id="sid",
        session_key="agent:main:discord:group:general:141",
        inbound_event_id=event_id,
        person_id=person_id,
        text="Is this a cron? Turn this off",
        authority="trusted",
    )

    tokens = set_session_vars(
        platform="hub",
        chat_id="hub:speculator",
        chat_type="dm",
        user_id="speculator",
        user_name="speculator",
        session_key="agent:main:hub:dm:hub:speculator",
        agent_event_id=event_id,
        person_id="hub:speculator",
    )
    try:
        decision = evaluate_send_message_policy(
            target_platform="discord",
            target_chat_id="general",
            message="Market digest",
            db=db,
        )
    finally:
        clear_session_vars(tokens)
        db.close()

    assert decision.allowed is False
    assert decision.policy == "suppress_public_broadcasts"


def test_cron_like_hub_inbound_blocks_public_rebroadcast(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    event_id = db.append_agent_event(
        event_type="inbound",
        event_subtype="message",
        status="received",
        session_key="agent:main:hub:dm:hub:speculator",
        source="hub",
        platform="hub",
        platform_chat_id="hub:speculator",
        content="Cronjob Response: synthetic market digest",
    )
    tokens = set_session_vars(
        platform="hub",
        chat_id="hub:speculator",
        chat_type="dm",
        user_id="speculator",
        user_name="speculator",
        session_key="agent:main:hub:dm:hub:speculator",
        agent_event_id=event_id,
        person_id="hub:speculator",
    )
    try:
        decision = evaluate_send_message_policy(
            target_platform="discord",
            target_chat_id="1495468809216327702",
            message="Synthetic market digest",
            db=db,
        )
    finally:
        clear_session_vars(tokens)
        db.close()

    assert decision.allowed is False
    assert decision.policy == "autonomous_public_rebroadcast_guard"


def test_cron_like_hub_inbound_blocks_signal_group_rebroadcast(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    event_id = db.append_agent_event(
        event_type="inbound",
        event_subtype="message",
        status="received",
        session_key="agent:main:hub:dm:hub:speculator",
        source="hub",
        platform="hub",
        platform_chat_id="hub:speculator",
        content="Cronjob Response: synthetic market digest",
    )
    tokens = set_session_vars(
        platform="hub",
        chat_id="hub:speculator",
        chat_type="dm",
        user_id="speculator",
        user_name="speculator",
        session_key="agent:main:hub:dm:hub:speculator",
        agent_event_id=event_id,
        person_id="hub:speculator",
    )
    try:
        decision = evaluate_send_message_policy(
            target_platform="signal",
            target_chat_id="signal-group",
            message="Synthetic market digest",
            db=db,
        )
    finally:
        clear_session_vars(tokens)
        db.close()

    assert decision.allowed is False
    assert decision.policy == "autonomous_public_rebroadcast_guard"
