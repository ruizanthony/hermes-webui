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
        stream_id="same-stream",
        turn_id="older-turn",
        submitted_prompt_text="older prompt",
    )


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
