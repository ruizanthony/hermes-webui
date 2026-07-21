import threading

import pytest

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
    with config.STREAM_SESSION_OWNERS_LOCK:
        config.STREAM_SESSION_OWNERS.pop(stream_id, None)


def _seed_preinsert_owner(stream_id, session_id):
    """Model route state after owner registration but before STREAMS insertion."""
    identity = {
        "session_id": session_id,
        "turn_id": "new-turn",
        "prompt_hash": submitted_prompt_sha256("new prompt"),
    }
    config.register_stream_owner(stream_id, session_id)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id] = dict(identity)
    with config.STREAMS_LOCK:
        config.STREAMS.pop(stream_id, None)
        config.CANCEL_FLAGS[stream_id] = "new-cancel"
        config.STREAM_PARTIAL_TEXT[stream_id] = "new-partial"
        config.STREAM_REASONING_TEXT[stream_id] = "new-reasoning"
        config.STREAM_LIVE_TOOL_CALLS[stream_id] = ["new-tool"]
    return identity


def _assert_preinsert_owner_unchanged(stream_id, session_id, identity):
    assert config.stream_owner_session_id(stream_id) == session_id
    assert stream_id not in config.STREAMS
    assert config.ACTIVE_RUNS[stream_id] == identity
    assert config.CANCEL_FLAGS[stream_id] == "new-cancel"
    assert config.STREAM_PARTIAL_TEXT[stream_id] == "new-partial"
    assert config.STREAM_REASONING_TEXT[stream_id] == "new-reasoning"
    assert config.STREAM_LIVE_TOOL_CALLS[stream_id] == ["new-tool"]


def test_delayed_legacy_worker_cannot_unregister_owner_before_channel_insert(tmp_path):
    stream_id = "preinsert-legacy-stream"
    session_id = "shared-legacy-session"
    identity = _seed_preinsert_owner(stream_id, session_id)

    try:
        streaming._run_agent_streaming(
            session_id,
            "old prompt",
            "old-model",
            str(tmp_path),
            stream_id,
            [],
            turn_id="old-turn",
            prompt_hash=submitted_prompt_sha256("old prompt"),
        )
        _assert_preinsert_owner_unchanged(stream_id, session_id, identity)
    finally:
        _cleanup(stream_id)


def test_delayed_gateway_worker_cannot_unregister_owner_before_channel_insert(tmp_path):
    stream_id = "preinsert-gateway-stream"
    session_id = "shared-gateway-session"
    identity = _seed_preinsert_owner(stream_id, session_id)

    try:
        gateway_chat._run_gateway_chat_streaming(
            session_id,
            "old prompt",
            "old-model",
            str(tmp_path),
            stream_id,
            [],
            turn_id="old-turn",
            prompt_hash=submitted_prompt_sha256("old prompt"),
        )
        _assert_preinsert_owner_unchanged(stream_id, session_id, identity)
    finally:
        _cleanup(stream_id)


@pytest.mark.parametrize(
    "runner",
    [streaming._run_agent_streaming, gateway_chat._run_gateway_chat_streaming],
    ids=["legacy", "gateway"],
)
def test_cancelled_route_generation_is_released_by_exact_worker_identity(
    runner, tmp_path
):
    """Cancel may detach STREAMS before the route-created worker starts."""
    stream_id = f"cancelled-before-start-{runner.__module__}"
    session_id = "cancelled-before-start-session"
    turn_id = "cancelled-before-start-turn"
    prompt = "cancelled before start prompt"
    prompt_hash = submitted_prompt_sha256(prompt)
    channel = object()
    assert config.register_stream_channel(
        stream_id,
        channel,
        session_id=session_id,
        turn_id=turn_id,
        prompt_hash=prompt_hash,
    )
    with config.STREAMS_LOCK:
        config.STREAMS.pop(stream_id, None)
        config.CANCEL_FLAGS[stream_id] = "old-cancel"
        config.STREAM_PARTIAL_TEXT[stream_id] = "old-partial"

    try:
        runner(
            session_id,
            prompt,
            "old-model",
            str(tmp_path),
            stream_id,
            [],
            stream=channel,
            turn_id=turn_id,
            prompt_hash=prompt_hash,
        )
        assert stream_id not in config.STREAM_SESSION_OWNERS
        assert stream_id not in config.ACTIVE_RUNS
        assert stream_id not in config.STREAMS
        assert stream_id not in config.CANCEL_FLAGS
        assert stream_id not in config.STREAM_PARTIAL_TEXT
    finally:
        _cleanup(stream_id)


class _OwnerLockHandoffProbe:
    """Pause an old teardown immediately before its post-ACTIVE owner cleanup."""

    def __init__(self, real_lock, stream_id, old_thread_id, observed, release):
        self._real_lock = real_lock
        self._stream_id = stream_id
        self._old_thread_id = old_thread_id
        self._observed = observed
        self._release = release

    def __enter__(self):
        if (
            threading.get_ident() == self._old_thread_id[0]
            and self._stream_id not in config.ACTIVE_RUNS
        ):
            self._observed.set()
            assert self._release.wait(timeout=2), "handoff probe was not released"
        self._real_lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._real_lock.release()


def test_active_run_owner_cleanup_is_atomic_across_replacement_handoff(monkeypatch):
    stream_id = "active-owner-handoff-stream"
    session_id = "shared-handoff-session"
    old_identity = {
        "session_id": session_id,
        "turn_id": "old-turn",
        "prompt_hash": "old-hash",
    }
    new_identity = {
        "session_id": session_id,
        "turn_id": "new-turn",
        "prompt_hash": "new-hash",
    }
    new_channel = object()
    observed = threading.Event()
    release = threading.Event()
    old_thread_id = [None]
    result = []
    real_owner_lock = config.STREAM_SESSION_OWNERS_LOCK

    config.register_stream_owner(stream_id, session_id)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id] = dict(old_identity)
    monkeypatch.setattr(
        config,
        "STREAM_SESSION_OWNERS_LOCK",
        _OwnerLockHandoffProbe(
            real_owner_lock, stream_id, old_thread_id, observed, release
        ),
    )

    def _old_unregister():
        old_thread_id[0] = threading.get_ident()
        result.append(
            config.unregister_active_run_if_matches(
                stream_id,
                turn_id=old_identity["turn_id"],
                prompt_hash=old_identity["prompt_hash"],
            )
        )

    thread = threading.Thread(target=_old_unregister)
    try:
        thread.start()
        if observed.wait(timeout=0.25):
            config.register_stream_owner(stream_id, session_id)
            with config.ACTIVE_RUNS_LOCK:
                config.ACTIVE_RUNS[stream_id] = dict(new_identity)
            with config.STREAMS_LOCK:
                config.STREAMS[stream_id] = new_channel
                config.CANCEL_FLAGS[stream_id] = "new-cancel"
                config.STREAM_PARTIAL_TEXT[stream_id] = "new-partial"
            release.set()
            thread.join(timeout=2)
        else:
            # An atomic implementation reaches owner cleanup while the old
            # ACTIVE_RUNS row still exists, so the split-window probe cannot fire.
            thread.join(timeout=2)
            config.register_stream_owner(stream_id, session_id)
            with config.ACTIVE_RUNS_LOCK:
                config.ACTIVE_RUNS[stream_id] = dict(new_identity)
            with config.STREAMS_LOCK:
                config.STREAMS[stream_id] = new_channel
                config.CANCEL_FLAGS[stream_id] = "new-cancel"
                config.STREAM_PARTIAL_TEXT[stream_id] = "new-partial"

        assert not thread.is_alive()
        assert result == [True]
        assert config.stream_owner_session_id(stream_id) == session_id
        assert config.ACTIVE_RUNS[stream_id] == new_identity
        assert config.STREAMS[stream_id] is new_channel
        assert config.CANCEL_FLAGS[stream_id] == "new-cancel"
        assert config.STREAM_PARTIAL_TEXT[stream_id] == "new-partial"
    finally:
        release.set()
        thread.join(timeout=2)
        monkeypatch.setattr(config, "STREAM_SESSION_OWNERS_LOCK", real_owner_lock)
        _cleanup(stream_id)


class _RouteSession:
    def __init__(self, session_id, workspace):
        self.session_id = session_id
        self.workspace = str(workspace)
        self.model = "route-model"
        self.model_provider = "route-provider"
        self.profile = "default"
        self.messages = [{"role": "user", "content": "history"}]
        self.title = "Untitled"
        self.active_stream_id = None

    def save(self, *args, **kwargs):
        return None


def _patch_route_start_dependencies(monkeypatch, parent, child, *, background):
    from api import background as background_api
    from api import models, routes

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kwargs: None)
    monkeypatch.setattr(routes, "get_session", lambda session_id: parent)
    monkeypatch.setattr(models, "new_session", lambda **kwargs: child)
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kwargs: payload)
    if background:
        monkeypatch.setattr(background_api, "track_background", lambda *args: None)
        monkeypatch.setattr(background_api, "complete_background", lambda *args: None)
    else:
        monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda session_id: False)
        monkeypatch.setattr(background_api, "track_btw", lambda *args: None)
    return routes, models


def _assert_route_owner_identity(stream_id, channel, session_id, turn_id, prompt_hash):
    with config.STREAM_SESSION_OWNERS_LOCK:
        owner = config.STREAM_SESSION_OWNERS[stream_id]
        assert owner["stream"] is channel
        assert owner["session_id"] == session_id
        assert owner["turn_id"] == turn_id
        assert owner["prompt_hash"] == prompt_hash


def test_btw_worker_receives_exact_route_channel_and_complete_identity(monkeypatch, tmp_path):
    parent = _RouteSession("btw-parent", tmp_path)
    child = _RouteSession("btw-child", tmp_path)
    routes, _models = _patch_route_start_dependencies(
        monkeypatch, parent, child, background=False
    )
    channel = object()
    captured = {}

    class _DeferredThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            captured.update(target=target, args=args, kwargs=kwargs or {}, daemon=daemon)

        def start(self):
            return None

    monkeypatch.setattr(routes, "create_stream_channel", lambda: channel)
    monkeypatch.setattr(routes.threading, "Thread", _DeferredThread)

    response = routes._handle_btw(
        object(), {"session_id": parent.session_id, "question": "side question"}
    )
    stream_id = response["stream_id"]
    try:
        assert captured["kwargs"]["stream"] is channel
        turn_id = captured["kwargs"]["turn_id"]
        prompt_hash = captured["kwargs"]["prompt_hash"]
        assert turn_id
        assert prompt_hash == submitted_prompt_sha256("side question")
        assert config.STREAMS[stream_id] is channel
        _assert_route_owner_identity(
            stream_id, channel, child.session_id, turn_id, prompt_hash
        )
    finally:
        _cleanup(stream_id)


def test_background_worker_receives_exact_route_channel_and_complete_identity(
    monkeypatch, tmp_path
):
    parent = _RouteSession("background-parent", tmp_path)
    child = _RouteSession("background-child", tmp_path)
    child.messages.append({"role": "assistant", "content": "answer"})
    routes, models = _patch_route_start_dependencies(
        monkeypatch, parent, child, background=True
    )
    channel = object()
    captured = {}

    class _ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    def _record_worker(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)

    monkeypatch.setattr(routes, "create_stream_channel", lambda: channel)
    monkeypatch.setattr(routes, "_run_agent_streaming", _record_worker)
    monkeypatch.setattr(routes.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(models.Session, "load", staticmethod(lambda session_id: child))
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)

    response = routes._handle_background(
        object(), {"session_id": parent.session_id, "prompt": "background prompt"}
    )
    stream_id = response["stream_id"]
    try:
        assert captured["kwargs"]["stream"] is channel
        turn_id = captured["kwargs"]["turn_id"]
        prompt_hash = captured["kwargs"]["prompt_hash"]
        assert turn_id
        assert prompt_hash == submitted_prompt_sha256("background prompt")
        assert config.STREAMS[stream_id] is channel
        _assert_route_owner_identity(
            stream_id, channel, child.session_id, turn_id, prompt_hash
        )
    finally:
        _cleanup(stream_id)


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
