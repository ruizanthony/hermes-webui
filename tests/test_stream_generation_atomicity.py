from __future__ import annotations

import io
import queue
import sys
import threading
import time
import types
from unittest import mock
from urllib.parse import urlparse

import pytest

from api import config, gateway_chat, models, routes, streaming
from api.active_checkpoint import build_active_checkpoint, submitted_prompt_sha256
from api.metering import meter


@pytest.fixture(autouse=True)
def _isolated_generation_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    for mapping in (
        config.STREAMS,
        config.STREAM_SESSION_OWNERS,
        config.ACTIVE_RUNS,
        config.CANCEL_FLAGS,
        config.AGENT_INSTANCES,
        config.STREAM_PARTIAL_TEXT,
        config.STREAM_REASONING_TEXT,
        config.STREAM_LIVE_TOOL_CALLS,
        config.STREAM_GOAL_RELATED,
        config.STREAM_LAST_EVENT_ID,
        gateway_chat._STREAM_RUN_IDS,
        gateway_chat._STREAM_RUN_CHANNELS,
        models.SESSIONS,
    ):
        mapping.clear()
    with meter()._lock:
        meter()._sessions.clear()
    yield
    for mapping in (
        config.STREAMS,
        config.STREAM_SESSION_OWNERS,
        config.ACTIVE_RUNS,
        config.CANCEL_FLAGS,
        config.AGENT_INSTANCES,
        config.STREAM_PARTIAL_TEXT,
        config.STREAM_REASONING_TEXT,
        config.STREAM_LIVE_TOOL_CALLS,
        config.STREAM_GOAL_RELATED,
        config.STREAM_LAST_EVENT_ID,
        gateway_chat._STREAM_RUN_IDS,
        gateway_chat._STREAM_RUN_CHANNELS,
        models.SESSIONS,
    ):
        mapping.clear()
    with meter()._lock:
        meter()._sessions.clear()


@pytest.fixture
def _mock_hermes_modules():
    runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = lambda requested=None, **kwargs: {
        "provider": requested or kwargs.get("target_provider") or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.runtime_provider = runtime_module
    hermes_state = types.ModuleType("hermes_state")
    hermes_state.SessionDB = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": hermes_cli,
        "hermes_cli.runtime_provider": runtime_module,
        "hermes_state": hermes_state,
    }
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in injected}
    sys.modules.update(injected)
    yield
    for name, previous in saved.items():
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def _session(session_id, stream_id, turn_id, prompt, workspace):
    session = models.Session(session_id=session_id, title="Generation test")
    session.workspace = str(workspace)
    session.model = "test-model"
    session.model_provider = "test-provider"
    session.profile = "default"
    session.pending_user_message = prompt
    session.pending_attachments = []
    session.pending_started_at = time.time()
    session.pending_turn_id = turn_id
    session.active_stream_id = stream_id
    session.active_checkpoint = build_active_checkpoint(
        stream_id=stream_id,
        turn_id=turn_id,
        submitted_prompt_text=prompt,
    )
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _register(stream_id, channel, session_id, turn_id, prompt, *, profile="default"):
    return config.register_stream_channel(
        stream_id,
        channel,
        session_id=session_id,
        turn_id=turn_id,
        prompt_hash=submitted_prompt_sha256(prompt),
        profile=profile,
    )


def _install_replacement(stream_id, session, workspace):
    new_prompt = "replacement prompt"
    new_turn = "replacement-turn"
    new_channel = config.create_stream_channel()
    assert _register(
        stream_id,
        new_channel,
        session.session_id,
        new_turn,
        new_prompt,
        profile="other",
    )
    session.active_stream_id = stream_id
    session.pending_user_message = new_prompt
    session.pending_turn_id = new_turn
    session.active_checkpoint = build_active_checkpoint(
        stream_id=stream_id,
        turn_id=new_turn,
        submitted_prompt_text=new_prompt,
    )
    session.workspace = str(workspace)
    replacement_cancel = threading.Event()
    replacement_agent = object()
    config.CANCEL_FLAGS[stream_id] = replacement_cancel
    config.AGENT_INSTANCES[stream_id] = replacement_agent
    config.STREAM_PARTIAL_TEXT[stream_id] = "replacement-partial"
    config.STREAM_REASONING_TEXT[stream_id] = "replacement-reasoning"
    config.STREAM_LIVE_TOOL_CALLS[stream_id] = [{"name": "replacement-tool"}]
    config.STREAM_GOAL_RELATED[stream_id] = True
    config.STREAM_LAST_EVENT_ID[stream_id] = "replacement-event"
    meter().begin_session(stream_id)
    return new_channel, replacement_cancel, replacement_agent


def _assert_replacement_intact(
    stream_id, new_channel, replacement_cancel, replacement_agent
):
    assert config.STREAMS[stream_id] is new_channel
    assert config.CANCEL_FLAGS[stream_id] is replacement_cancel
    assert not replacement_cancel.is_set()
    assert config.AGENT_INSTANCES[stream_id] is replacement_agent
    assert config.STREAM_PARTIAL_TEXT[stream_id] == "replacement-partial"
    assert config.STREAM_REASONING_TEXT[stream_id] == "replacement-reasoning"
    assert config.STREAM_LIVE_TOOL_CALLS[stream_id] == [
        {"name": "replacement-tool"}
    ]
    assert config.STREAM_GOAL_RELATED[stream_id] is True
    assert config.STREAM_LAST_EVENT_ID[stream_id] == "replacement-event"
    assert config.ACTIVE_RUNS[stream_id]["turn_id"] == "replacement-turn"
    assert config.STREAM_SESSION_OWNERS[stream_id]["stream"] is new_channel
    assert stream_id in meter()._sessions


class _BaseAgent:
    def __init__(
        self,
        *args,
        session_id=None,
        stream_delta_callback=None,
        reasoning_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        **kwargs,
    ):
        self.session_id = session_id
        self.stream_delta_callback = stream_delta_callback
        self.reasoning_callback = reasoning_callback
        self.tool_progress_callback = tool_progress_callback
        self.tool_start_callback = tool_start_callback
        self.tool_complete_callback = tool_complete_callback
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.context_compressor = None
        self._last_error = None
        self.ephemeral_system_prompt = None

    def interrupt(self, _message):
        return None


def _run_legacy_with_agent(monkeypatch, agent_cls, session, channel, turn_id, prompt):
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: agent_cls)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *args, **kwargs: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    streaming._run_agent_streaming(
        session.session_id,
        prompt,
        "test-model",
        session.workspace,
        session.active_stream_id,
        [],
        stream=channel,
        turn_id=turn_id,
        prompt_hash=submitted_prompt_sha256(prompt),
    )


def test_legacy_post_admission_initialization_cannot_overwrite_replacement(
    monkeypatch, tmp_path, _mock_hermes_modules
):
    stream_id = "legacy-initial-race"
    turn_id = "old-turn"
    prompt = "old prompt"
    session = _session("legacy_initial_session", stream_id, turn_id, prompt, tmp_path)
    old_channel = config.create_stream_channel()
    assert _register(stream_id, old_channel, session.session_id, turn_id, prompt)
    admitted = threading.Event()
    resume = threading.Event()
    real_register = config.register_active_run

    def paused_register(*args, **kwargs):
        result = real_register(*args, **kwargs)
        admitted.set()
        assert resume.wait(3)
        return result

    class FailingAgent(_BaseAgent):
        def run_conversation(self, **kwargs):
            raise RuntimeError("stop after initialization")

    monkeypatch.setattr(streaming, "register_active_run", paused_register)
    worker = threading.Thread(
        target=_run_legacy_with_agent,
        args=(monkeypatch, FailingAgent, session, old_channel, turn_id, prompt),
    )
    worker.start()
    assert admitted.wait(3)
    replacement = _install_replacement(stream_id, session, tmp_path)
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    _assert_replacement_intact(stream_id, *replacement)


def test_legacy_late_callbacks_and_terminal_cleanup_cannot_touch_replacement(
    monkeypatch, tmp_path, _mock_hermes_modules
):
    stream_id = "legacy-late-race"
    turn_id = "old-turn"
    prompt = "old prompt"
    session = _session("legacy_late_session", stream_id, turn_id, prompt, tmp_path)
    old_channel = config.create_stream_channel()
    assert _register(stream_id, old_channel, session.session_id, turn_id, prompt)
    running = threading.Event()
    resume = threading.Event()

    class PausedAgent(_BaseAgent):
        def run_conversation(self, **kwargs):
            running.set()
            assert resume.wait(3)
            self.stream_delta_callback("old-token")
            self.reasoning_callback("old-reasoning")
            self.tool_start_callback("old-tool-id", "terminal", {"cmd": "old"})
            self.tool_complete_callback(
                "old-tool-id", "terminal", {"cmd": "old"}, "old-result"
            )
            raise RuntimeError("old terminal error")

    worker = threading.Thread(
        target=_run_legacy_with_agent,
        args=(monkeypatch, PausedAgent, session, old_channel, turn_id, prompt),
    )
    worker.start()
    assert running.wait(3)
    replacement = _install_replacement(stream_id, session, tmp_path)
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    _assert_replacement_intact(stream_id, *replacement)


def test_gateway_post_admission_initialization_cannot_overwrite_replacement(
    monkeypatch, tmp_path
):
    stream_id = "gateway-initial-race"
    turn_id = "old-turn"
    prompt = "old prompt"
    session = _session("gateway_initial_session", stream_id, turn_id, prompt, tmp_path)
    old_channel = config.create_stream_channel()
    assert _register(stream_id, old_channel, session.session_id, turn_id, prompt)
    admitted = threading.Event()
    resume = threading.Event()
    real_register = config.register_active_run

    def paused_register(*args, **kwargs):
        result = real_register(*args, **kwargs)
        admitted.set()
        assert resume.wait(3)
        return result

    monkeypatch.setattr(gateway_chat, "register_active_run", paused_register)
    monkeypatch.setattr(
        gateway_chat,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(RuntimeError("stop after initialization")),
    )
    worker = threading.Thread(
        target=gateway_chat._run_gateway_chat_streaming,
        args=(
            session.session_id,
            prompt,
            "test-model",
            str(tmp_path),
            stream_id,
            [],
        ),
        kwargs={
            "stream": old_channel,
            "turn_id": turn_id,
            "prompt_hash": submitted_prompt_sha256(prompt),
        },
    )
    worker.start()
    assert admitted.wait(3)
    replacement = _install_replacement(stream_id, session, tmp_path)
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    _assert_replacement_intact(stream_id, *replacement)


def test_gateway_late_callbacks_and_terminal_cleanup_cannot_touch_replacement(
    monkeypatch, tmp_path
):
    stream_id = "gateway-late-race"
    turn_id = "old-turn"
    prompt = "old prompt"
    session = _session("gateway_late_session", stream_id, turn_id, prompt, tmp_path)
    old_channel = config.create_stream_channel()
    assert _register(stream_id, old_channel, session.session_id, turn_id, prompt)
    reading = threading.Event()
    resume = threading.Event()

    class PausedResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            reading.set()
            assert resume.wait(3)
            yield b'event: hermes.tool.progress\n'
            yield b'data: {"tool":"terminal","toolCallId":"old-tool","status":"running"}\n\n'
            yield b'event: reasoning.available\n'
            yield b'data: {"text":"old-reasoning"}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"old-token"}}]}\n\n'
            raise RuntimeError("old gateway terminal error")

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.invalid")
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *args: False)
    monkeypatch.setattr(
        gateway_chat, "gateway_approval_unavailable_reason", lambda *args: None
    )
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda *a, **k: PausedResponse())
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(streaming, "_normalize_prefill_messages_before_user_turn", lambda rows: rows)
    monkeypatch.setattr(streaming, "_webui_ephemeral_system_prompt", lambda *a, **k: "system")
    worker = threading.Thread(
        target=gateway_chat._run_gateway_chat_streaming,
        args=(
            session.session_id,
            prompt,
            "test-model",
            str(tmp_path),
            stream_id,
            [],
        ),
        kwargs={
            "stream": old_channel,
            "turn_id": turn_id,
            "prompt_hash": submitted_prompt_sha256(prompt),
        },
    )
    worker.start()
    assert reading.wait(3)
    replacement = _install_replacement(stream_id, session, tmp_path)
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    _assert_replacement_intact(stream_id, *replacement)


class _Handler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        return None


class _TrackedChannel:
    def __init__(self):
        self.subscribed = False
        self.items = queue.Queue()
        self.items.put_nowait(("stream_end", {"ok": True}))

    def subscribe_with_snapshot(self):
        self.subscribed = True
        return self.items, {}

    def unsubscribe(self, _subscriber):
        return None


def _capture_json(monkeypatch):
    captured = {}

    def fake_j(_handler, payload, status=200, **kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    def fake_bad(_handler, message, status=400):
        captured["bad"] = (message, status)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    return captured


def test_sse_authorization_cannot_subscribe_replacement_channel(monkeypatch):
    stream_id = "sse-auth-race"
    old_channel = _TrackedChannel()
    new_channel = _TrackedChannel()
    assert _register(stream_id, old_channel, "old_session", "old-turn", "old prompt")

    authorized = threading.Event()
    resume = threading.Event()

    def pause_after_owner_check(_owner_profile, _active_profile):
        authorized.set()
        assert resume.wait(3)
        return True

    def install_replacement():
        assert _register(
            stream_id,
            new_channel,
            "foreign_session",
            "new-turn",
            "new prompt",
            profile="other",
        )

    monkeypatch.setattr(routes, "_profiles_match", pause_after_owner_check)
    monkeypatch.setattr(
        routes, "_sse_replay_run_journal_gap_checked", lambda *a, **k: (False, None)
    )
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    request = threading.Thread(
        target=routes._handle_sse_stream,
        args=(_Handler(), urlparse(f"/api/chat/stream?stream_id={stream_id}")),
    )
    request.start()
    assert authorized.wait(3)
    replacement = threading.Thread(target=install_replacement)
    replacement.start()
    assert config.STREAMS[stream_id] is old_channel
    resume.set()
    request.join(3)
    replacement.join(3)
    assert not request.is_alive()
    assert not replacement.is_alive()

    assert old_channel.subscribed is True
    assert new_channel.subscribed is False


def test_status_authorization_cannot_observe_replacement_generation(monkeypatch):
    stream_id = "status-auth-race"
    old_channel = _TrackedChannel()
    new_channel = _TrackedChannel()
    assert _register(stream_id, old_channel, "old_session", "old-turn", "old prompt")
    with config.STREAMS_LOCK:
        config.STREAMS.pop(stream_id, None)

    authorized = threading.Event()
    resume = threading.Event()

    def pause_after_owner_check(_owner_profile, _active_profile):
        authorized.set()
        assert resume.wait(3)
        return True

    def install_replacement():
        assert _register(
            stream_id,
            new_channel,
            "foreign_session",
            "new-turn",
            "new prompt",
            profile="other",
        )

    monkeypatch.setattr(routes, "_profiles_match", pause_after_owner_check)
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: None)
    captured = _capture_json(monkeypatch)
    request = threading.Thread(
        target=routes.handle_get,
        args=(
            _Handler(),
            urlparse(f"/api/chat/stream/status?stream_id={stream_id}"),
        ),
    )
    request.start()
    assert authorized.wait(3)
    replacement = threading.Thread(target=install_replacement)
    replacement.start()
    resume.set()
    request.join(3)
    replacement.join(3)
    assert not request.is_alive()
    assert not replacement.is_alive()

    assert captured["payload"]["active"] is False
    assert config.STREAMS[stream_id] is new_channel


def test_cancel_authorization_cannot_cancel_replacement_generation(monkeypatch):
    from api import runtime_adapter

    stream_id = "cancel-auth-race"
    old_channel = _TrackedChannel()
    new_channel = _TrackedChannel()
    assert _register(stream_id, old_channel, "old_session", "old-turn", "old prompt")
    new_cancel = threading.Event()
    real_cancel = streaming.cancel_stream

    def replace_before_cancel(_stream_id, **kwargs):
        assert _register(
            stream_id,
            new_channel,
            "foreign_session",
            "new-turn",
            "new prompt",
            profile="other",
        )
        config.CANCEL_FLAGS[stream_id] = new_cancel
        return real_cancel(_stream_id, **kwargs)

    monkeypatch.setattr(routes, "cancel_stream", replace_before_cancel)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    captured = _capture_json(monkeypatch)
    routes.handle_get(
        _Handler(), urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    )

    assert captured["payload"]["cancelled"] is False
    assert config.STREAMS[stream_id] is new_channel
    assert config.CANCEL_FLAGS[stream_id] is new_cancel
    assert not new_cancel.is_set()
