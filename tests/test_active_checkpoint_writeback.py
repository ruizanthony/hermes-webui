from api.active_checkpoint import (
    active_checkpoint_matches,
    build_active_checkpoint,
    submitted_prompt_sha256,
)
from api.models import Session


def test_prompt_hash_normalizes_unicode_and_line_endings_only():
    assert submitted_prompt_sha256("Cafe\u0301\r\nline\r") == submitted_prompt_sha256("Café\nline\n")
    assert submitted_prompt_sha256(" prompt") != submitted_prompt_sha256("prompt")


def test_active_checkpoint_round_trips_with_session_state(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)

    checkpoint = build_active_checkpoint(
        stream_id="stream-current",
        turn_id="turn-current",
        submitted_prompt_text=" exact prompt \n",
    )
    session = Session(
        session_id="checkpoint_roundtrip",
        workspace=str(tmp_path),
        active_stream_id="stream-current",
        active_checkpoint=checkpoint,
        pending_turn_id="turn-current",
    )
    session.save()

    loaded = Session.load(session.session_id)
    assert loaded.active_checkpoint == checkpoint
    assert loaded.pending_turn_id == "turn-current"


def test_checkpoint_match_requires_stream_turn_and_prompt_hash():
    prompt = "current prompt"
    session = Session(
        session_id="checkpoint_match",
        active_stream_id="same-stream",
        active_checkpoint=build_active_checkpoint(
            stream_id="same-stream",
            turn_id="current-turn",
            submitted_prompt_text=prompt,
        ),
    )

    assert active_checkpoint_matches(
        session,
        stream_id="same-stream",
        turn_id="current-turn",
        submitted_prompt_text=prompt,
    )
    assert not active_checkpoint_matches(
        session,
        stream_id="wrong-stream",
        turn_id="current-turn",
        submitted_prompt_text=prompt,
    )
    assert not active_checkpoint_matches(
        session,
        stream_id="same-stream",
        turn_id="wrong-turn",
        submitted_prompt_text=prompt,
    )
    assert not active_checkpoint_matches(
        session,
        stream_id="same-stream",
        turn_id="current-turn",
        submitted_prompt_text="wrong prompt",
    )


def test_checkpoint_match_also_requires_session_active_stream_id():
    prompt = "current prompt"
    session = Session(
        session_id="checkpoint_active_stream",
        active_stream_id="replacement-stream",
        active_checkpoint=build_active_checkpoint(
            stream_id="old-stream",
            turn_id="old-turn",
            submitted_prompt_text=prompt,
        ),
    )

    assert not active_checkpoint_matches(
        session,
        stream_id="old-stream",
        turn_id="old-turn",
        submitted_prompt_text=prompt,
    )


def test_checkpoint_clear_helper_clears_both_ownership_fields():
    from api.active_checkpoint import clear_active_checkpoint

    session = Session(session_id="checkpoint_clear")
    session.active_checkpoint = {"stream_id": "s", "turn_id": "t", "prompt_hash": "h"}
    session.pending_turn_id = "t"

    clear_active_checkpoint(session)

    assert session.active_checkpoint is None
    assert session.pending_turn_id is None


def test_chat_start_preparation_persists_checkpoint_identity(tmp_path, monkeypatch):
    import api.routes as routes

    session = Session(session_id="checkpoint_start", workspace=str(tmp_path))
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")

    routes._prepare_chat_start_session_for_stream(
        session,
        msg="trimmed prompt",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-start",
        turn_id="turn-start",
        submitted_prompt_text="  trimmed prompt\r\n",
    )

    assert saved == [True]
    assert session.pending_turn_id == "turn-start"
    assert session.active_checkpoint == {
        "stream_id": "stream-start",
        "turn_id": "turn-start",
        "prompt_hash": submitted_prompt_sha256("  trimmed prompt\r\n"),
    }


def test_gateway_terminal_cleanup_does_not_clear_newer_checkpoint():
    import api.gateway_chat as gateway_chat

    session = Session(session_id="gateway_checkpoint", active_stream_id="shared-stream")
    session.active_checkpoint = build_active_checkpoint(
        stream_id="shared-stream",
        turn_id="newer-turn",
        submitted_prompt_text="newer prompt",
    )
    session.pending_turn_id = "newer-turn"
    session.pending_user_message = "newer prompt"
    session.save = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale cleanup saved"))

    gateway_chat._clear_gateway_pending_state(
        session,
        "shared-stream",
        turn_id="older-turn",
        submitted_prompt_text="older prompt",
    )

    assert session.pending_user_message == "newer prompt"
    assert session.active_checkpoint["turn_id"] == "newer-turn"


def test_owned_gateway_cleanup_clears_complete_checkpoint():
    import api.gateway_chat as gateway_chat

    prompt = "owned gateway prompt"
    session = Session(session_id="gateway_owned_cleanup", active_stream_id="gateway-stream")
    session.active_checkpoint = build_active_checkpoint(
        stream_id="gateway-stream", turn_id="gateway-turn", submitted_prompt_text=prompt
    )
    session.pending_turn_id = "gateway-turn"
    session.pending_user_message = prompt
    session.save = lambda *args, **kwargs: None

    gateway_chat._clear_gateway_pending_state(
        session,
        "gateway-stream",
        turn_id="gateway-turn",
        submitted_prompt_text=prompt,
    )

    assert session.active_stream_id is None
    assert session.active_checkpoint is None
    assert session.pending_turn_id is None


def test_owned_legacy_cancel_cleanup_clears_complete_checkpoint():
    from api.streaming import _persist_cancelled_turn

    session = Session(session_id="legacy_owned_cancel", active_stream_id="legacy-stream")
    session.active_checkpoint = build_active_checkpoint(
        stream_id="legacy-stream", turn_id="legacy-turn", submitted_prompt_text="prompt"
    )
    session.pending_turn_id = "legacy-turn"
    session.pending_user_message = "prompt"
    session.messages = []
    session.save = lambda *args, **kwargs: None

    _persist_cancelled_turn(session)

    assert session.active_stream_id is None
    assert session.active_checkpoint is None
    assert session.pending_turn_id is None


def test_legacy_teardown_does_not_remove_reused_stream_globals():
    import api.config as config
    import api.streaming as streaming

    old_channel = object()
    new_channel = object()
    stream_id = "reused-legacy-stream"
    with streaming.STREAMS_LOCK:
        streaming.STREAMS[stream_id] = new_channel
        streaming.CANCEL_FLAGS[stream_id] = "new-cancel"
        streaming.STREAM_PARTIAL_TEXT[stream_id] = "new partial"
    config.register_active_run(
        stream_id, session_id="sid", turn_id="new-turn", prompt_hash="new-hash"
    )

    streaming._teardown_stream_globals_if_owned(
        stream_id, old_channel, turn_id="old-turn", prompt_hash="old-hash"
    )

    assert streaming.STREAMS[stream_id] is new_channel
    assert streaming.CANCEL_FLAGS[stream_id] == "new-cancel"
    assert streaming.STREAM_PARTIAL_TEXT[stream_id] == "new partial"
    assert config.ACTIVE_RUNS[stream_id]["turn_id"] == "new-turn"


def test_gateway_teardown_does_not_remove_reused_stream_globals():
    import api.config as config
    import api.gateway_chat as gateway_chat

    old_channel = object()
    new_channel = object()
    stream_id = "reused-gateway-stream"
    with gateway_chat.STREAMS_LOCK:
        gateway_chat.STREAMS[stream_id] = new_channel
        gateway_chat.CANCEL_FLAGS[stream_id] = "new-cancel"
        gateway_chat.STREAM_PARTIAL_TEXT[stream_id] = "new partial"
    gateway_chat._STREAM_RUN_IDS[stream_id] = "new-run-id"
    config.register_active_run(
        stream_id, session_id="sid", turn_id="new-turn", prompt_hash="new-hash"
    )

    gateway_chat._teardown_gateway_stream_globals_if_owned(
        stream_id,
        old_channel,
        turn_id="old-turn",
        prompt_hash="old-hash",
    )

    assert gateway_chat.STREAMS[stream_id] is new_channel
    assert gateway_chat.CANCEL_FLAGS[stream_id] == "new-cancel"
    assert gateway_chat.STREAM_PARTIAL_TEXT[stream_id] == "new partial"
    assert gateway_chat._STREAM_RUN_IDS[stream_id] == "new-run-id"
    assert config.ACTIVE_RUNS[stream_id]["turn_id"] == "new-turn"
