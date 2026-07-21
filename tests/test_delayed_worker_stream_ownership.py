from api import config, gateway_chat, streaming
from api.active_checkpoint import submitted_prompt_sha256


def _seed_reused_stream(stream_id, session_id):
    old_stream = object()
    newer_stream = object()
    newer_identity = {
        "session_id": session_id,
        "turn_id": "new-turn",
        "prompt_hash": submitted_prompt_sha256("new prompt"),
    }
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = newer_stream
        config.CANCEL_FLAGS[stream_id] = "new-cancel"
        config.STREAM_PARTIAL_TEXT[stream_id] = "new-partial"
        config.STREAM_REASONING_TEXT[stream_id] = "new-reasoning"
        config.STREAM_LIVE_TOOL_CALLS[stream_id] = ["new-tool"]
        config.STREAM_GOAL_RELATED[stream_id] = True
        config.STREAM_LAST_EVENT_ID[stream_id] = "new-event"
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id] = dict(newer_identity)
    config.register_stream_owner(stream_id, session_id)
    return old_stream, newer_stream, newer_identity


def _assert_newer_state_unchanged(stream_id, newer_stream, newer_identity, session_id):
    assert config.STREAMS[stream_id] is newer_stream
    assert config.CANCEL_FLAGS[stream_id] == "new-cancel"
    assert config.STREAM_PARTIAL_TEXT[stream_id] == "new-partial"
    assert config.STREAM_REASONING_TEXT[stream_id] == "new-reasoning"
    assert config.STREAM_LIVE_TOOL_CALLS[stream_id] == ["new-tool"]
    assert config.STREAM_GOAL_RELATED[stream_id] is True
    assert config.STREAM_LAST_EVENT_ID[stream_id] == "new-event"
    assert config.ACTIVE_RUNS[stream_id] == newer_identity
    assert config.stream_owner_session_id(stream_id) == session_id


def _cleanup(stream_id):
    with config.STREAMS_LOCK:
        config.STREAMS.pop(stream_id, None)
        config.CANCEL_FLAGS.pop(stream_id, None)
        config.STREAM_PARTIAL_TEXT.pop(stream_id, None)
        config.STREAM_REASONING_TEXT.pop(stream_id, None)
        config.STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)
        config.STREAM_GOAL_RELATED.pop(stream_id, None)
        config.STREAM_LAST_EVENT_ID.pop(stream_id, None)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(stream_id, None)
    config.unregister_stream_owner(stream_id)


def test_delayed_legacy_worker_cannot_adopt_reused_stream(monkeypatch, tmp_path):
    stream_id = "reused-legacy-stream"
    session_id = "new-legacy-session"
    old_stream, newer_stream, newer_identity = _seed_reused_stream(stream_id, session_id)
    provider_calls = []
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda *a, **k: provider_calls.append(True))

    try:
        streaming._run_agent_streaming(
            "old-legacy-session",
            "old prompt",
            "old-model",
            str(tmp_path),
            stream_id,
            [],
            stream=old_stream,
            turn_id="old-turn",
            prompt_hash=submitted_prompt_sha256("old prompt"),
        )
        _assert_newer_state_unchanged(stream_id, newer_stream, newer_identity, session_id)
        assert provider_calls == []
    finally:
        _cleanup(stream_id)


def test_delayed_gateway_worker_cannot_adopt_reused_stream(monkeypatch, tmp_path):
    stream_id = "reused-gateway-stream"
    session_id = "new-gateway-session"
    old_stream, newer_stream, newer_identity = _seed_reused_stream(stream_id, session_id)
    provider_calls = []
    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda *a, **k: provider_calls.append(True),
    )

    try:
        gateway_chat._run_gateway_chat_streaming(
            "old-gateway-session",
            "old prompt",
            "old-model",
            str(tmp_path),
            stream_id,
            [],
            stream=old_stream,
            turn_id="old-turn",
            prompt_hash=submitted_prompt_sha256("old prompt"),
        )
        _assert_newer_state_unchanged(stream_id, newer_stream, newer_identity, session_id)
        assert provider_calls == []
    finally:
        _cleanup(stream_id)


def test_active_run_registration_is_atomic_by_complete_owner():
    stream_id = "atomic-owner-stream"
    original = {
        "session_id": "session-a",
        "turn_id": "turn-a",
        "prompt_hash": "hash-a",
        "phase": "queued",
    }
    try:
        assert config.register_active_run(stream_id, **original) is True
        assert config.register_active_run(stream_id, **original, model="model-a") is True
        assert config.ACTIVE_RUNS[stream_id]["model"] == "model-a"
        assert config.register_active_run(
            stream_id,
            session_id="session-b",
            turn_id="turn-b",
            prompt_hash="hash-b",
        ) is False
        assert config.ACTIVE_RUNS[stream_id]["session_id"] == "session-a"
        assert config.ACTIVE_RUNS[stream_id]["turn_id"] == "turn-a"
        assert config.ACTIVE_RUNS[stream_id]["prompt_hash"] == "hash-a"
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.pop(stream_id, None)
