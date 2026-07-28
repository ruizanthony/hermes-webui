import json
import queue
import sys
import types
from pathlib import Path

import pytest

from api import models
from api import streaming
from api.models import Session


ROOT = Path(__file__).resolve().parents[1]


def _run_streaming_with_fake_agent(
    tmp_path,
    monkeypatch,
    agent_result,
    *,
    prior_messages=None,
    prior_context_messages=None,
    config=None,
    goal_related=False,
    agent_kwargs_out=None,
    msg_text="Do the long task.",
    step_counts=None,
    clear_agent_cache=True,
    stream_id="stream-tool-limit",
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    streaming.PENDING_GOAL_CONTINUATION.clear()
    try:
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        if clear_agent_cache:
            with SESSION_AGENT_CACHE_LOCK:
                SESSION_AGENT_CACHE.clear()
    except Exception:
        pass

    session_id = "tool_limit_session"
    session = Session(
        session_id=session_id,
        title="Tool limit test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=list(prior_messages or []),
        context_messages=list(prior_context_messages or []),
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[session_id] = session
    streaming.SESSIONS[session_id] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class FakeAgent:
        def __init__(self, max_iterations=None, step_callback=None, **kwargs):
            if agent_kwargs_out is not None:
                agent_kwargs_out.update(kwargs)
                agent_kwargs_out["max_iterations"] = max_iterations
                agent_kwargs_out["step_callback"] = step_callback
            self.session_id = kwargs.get("session_id")
            self.step_callback = step_callback
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, *_args, **_kwargs):
            for count in step_counts or []:
                if self.step_callback is not None:
                    self.step_callback(count, [])
            return agent_result

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        m.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
        m.setattr(streaming, "get_config", lambda *_args, **_kwargs: dict(config or {}))
        m.setattr("api.config.get_config_for_profile_home", lambda *_args, **_kwargs: dict(config or {}))
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text=msg_text,
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
            goal_related=goal_related,
        )

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    payload = json.loads((session_dir / f"{session_id}.json").read_text(encoding="utf-8"))
    return events, payload


def test_synthetic_max_iteration_summary_request_is_dropped_from_agent_result():
    synthetic = {
        "role": "user",
        "content": streaming._MAX_ITERATION_SUMMARY_REQUEST,
    }
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        synthetic,
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": messages,
    }

    assert streaming._agent_result_tool_limit_reached(result) is True

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(
        result["messages"],
        enabled=streaming._agent_result_tool_limit_reached(result),
    )

    assert synthetic not in cleaned
    assert cleaned[-1]["role"] == "assistant"
    assert "here is the summary" in cleaned[-1]["content"]


def test_tool_limit_detection_uses_explicit_boolean_grouping():
    streaming_py = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    assert "or ('tool-calling iterations' in haystack and 'maximum' in haystack)" in streaming_py


def test_continuous_iteration_policy_expands_validation_budget_with_bounded_rollovers():
    policy = streaming._continuous_iteration_policy(
        "/validation",
        {"agent": {"max_turns": 500}},
        goal_related=False,
    )

    assert policy == {
        "enabled": True,
        "base_limit": 500,
        "rollover_at": 400,
        "max_rollovers": 3,
        "effective_limit": 1600,
    }


def test_continuous_iteration_policy_stays_disabled_for_ordinary_chat():
    policy = streaming._continuous_iteration_policy(
        "Please inspect this small file.",
        {"agent": {"max_turns": 500}},
        goal_related=False,
    )

    assert policy["enabled"] is False
    assert policy["effective_limit"] == 500


@pytest.mark.parametrize("message", ["", "   ", None])
def test_continuous_iteration_policy_accepts_empty_messages(message):
    policy = streaming._continuous_iteration_policy(
        message,
        {"agent": {"max_turns": 500}},
        goal_related=False,
    )
    assert policy["enabled"] is False
    assert policy["effective_limit"] == 500


def test_continuous_iteration_policy_requires_explicit_base_limit():
    policy = streaming._continuous_iteration_policy(
        "/validation",
        {},
        goal_related=False,
    )
    assert policy["enabled"] is False
    assert policy["effective_limit"] is None


@pytest.mark.parametrize("max_rollovers", [0, 1, 2, 3, 99])
@pytest.mark.parametrize("threshold_ratio", [0.1, 0.5, 0.8, 0.99])
def test_continuous_iteration_policy_never_reduces_base_budget(max_rollovers, threshold_ratio):
    policy = streaming._continuous_iteration_policy(
        "/validation",
        {
            "agent": {"max_turns": 500},
            "webui": {"continuous_turns": {
                "max_rollovers": max_rollovers,
                "threshold_ratio": threshold_ratio,
            }},
        },
        goal_related=False,
    )
    assert policy["effective_limit"] >= 500


@pytest.mark.parametrize("enabled", [False, 0, "false", "no", None])
def test_continuous_iteration_policy_honors_disabled_values(enabled):
    policy = streaming._continuous_iteration_policy(
        "/validation",
        {
            "agent": {"max_turns": 500},
            "webui": {"continuous_turns": {"enabled": enabled}},
        },
        goal_related=False,
    )
    assert policy["enabled"] is False
    assert policy["effective_limit"] == 500


def test_continuous_iteration_policy_supports_goal_turn_and_bounded_overrides():
    policy = streaming._continuous_iteration_policy(
        "Continue the approved work.",
        {
            "agent": {"max_turns": 500},
            "webui": {
                "continuous_turns": {
                    "threshold_ratio": 0.75,
                    "max_rollovers": 2,
                }
            },
        },
        goal_related=True,
    )

    assert policy["enabled"] is True
    assert policy["rollover_at"] == 375
    assert policy["max_rollovers"] == 2
    assert policy["effective_limit"] == 1125


def test_iteration_rollover_boundaries_are_observable_and_bounded():
    policy = streaming._continuous_iteration_policy(
        "/validation",
        {"agent": {"max_turns": 500}},
        goal_related=False,
    )

    assert streaming._iteration_rollover_for_step(400, policy) is None
    assert streaming._iteration_rollover_for_step(401, policy) == 1
    assert streaming._iteration_rollover_for_step(801, policy) == 2
    assert streaming._iteration_rollover_for_step(1201, policy) == 3
    assert streaming._iteration_rollover_for_step(1601, policy) is None


def test_tool_limit_status_card_reports_exact_budget_and_preservation():
    messages = [
        {"role": "user", "content": "/validation"},
        {"role": "assistant", "content": "Checkpoint preserved."},
    ]
    result = {
        "turn_exit_reason": "max_iterations_reached(1600/1600)",
        "api_calls": 1600,
    }
    policy = {
        "enabled": True,
        "base_limit": 500,
        "rollover_at": 400,
        "max_rollovers": 3,
        "effective_limit": 1600,
    }

    assert streaming._mark_latest_assistant_tool_limit_status(
        messages,
        result=result,
        policy=policy,
    ) is True
    rows = messages[-1]["_statusCard"]["rows"]
    assert {row["label"]: row["value"] for row in rows} == {
        "State": "Limit reached",
        "Budget": "1600/1600",
        "Work preserved": "Yes",
        "Rollovers": "3/3",
        "Next step": "Start a new turn to continue.",
    }


def test_stream_constructs_agent_with_normal_and_continuous_limits(tmp_path, monkeypatch):
    captured = {}
    result = {
        "turn_exit_reason": "text_response(stop)",
        "final_response": "Validation complete.",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "Validation complete."},
        ],
    }

    _run_streaming_with_fake_agent(
        tmp_path / "normal",
        monkeypatch,
        result,
        config={"agent": {"max_turns": 500}},
        agent_kwargs_out=captured,
    )
    assert captured["max_iterations"] == 500

    captured.clear()
    _run_streaming_with_fake_agent(
        tmp_path / "continuous",
        monkeypatch,
        result,
        config={"agent": {"max_turns": 500}},
        goal_related=True,
        agent_kwargs_out=captured,
    )
    assert captured["max_iterations"] == 1600
    assert callable(captured["step_callback"])


def test_validation_stream_emits_bounded_rollover_events(tmp_path, monkeypatch):
    events, _payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        {"final_response": "done", "messages": []},
        config={"agent": {"max_turns": 500}},
        msg_text="/validation",
        step_counts=[400, 401, 401, 801, 1201, 1601],
    )
    rollovers = [payload for kind, payload in events if kind == "iteration_rollover"]
    assert [row["rollover"] for row in rollovers] == [1, 2, 3]
    assert [row["api_calls_completed"] for row in rollovers] == [400, 800, 1200]


def test_cached_agent_refreshes_rollover_callback_for_new_stream(tmp_path, monkeypatch):
    _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        {"final_response": "done", "messages": []},
        config={"agent": {"max_turns": 500}},
        msg_text="/validation",
        step_counts=[401],
        stream_id="stream-first",
    )
    events, _payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        {"final_response": "done", "messages": []},
        config={"agent": {"max_turns": 500}},
        msg_text="/validation",
        step_counts=[],
        clear_agent_cache=False,
        stream_id="stream-second",
    )
    rollovers = [payload for kind, payload in events if kind == "iteration_rollover"]
    assert [row["rollover"] for row in rollovers] == [1]


def test_rollover_frontend_handler_is_registered():
    source = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "source.addEventListener('iteration_rollover'" in source
    assert "Validation continues" in source


def test_historical_synthetic_summary_prompt_does_not_mark_normal_result_as_tool_limit():
    result = {
        "messages": [
            {"role": "user", "content": "Earlier task."},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "user", "content": "Current normal task."},
            {"role": "assistant", "content": "Current task completed normally."},
        ],
    }

    assert streaming._agent_result_tool_limit_reached(result) is False


def test_tool_limit_with_final_answer_marks_latest_assistant_status_card():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]

    assert streaming._session_lacks_final_assistant_answer(messages) is False
    assert streaming._mark_latest_assistant_tool_limit_status(messages) is True

    assistant = messages[-1]
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_terminal_reason"] == "max_iterations"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"


def test_tool_limit_without_final_answer_is_no_final_terminal_state_after_filtering():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
    ]

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(messages)

    assert all(
        not streaming._is_synthetic_max_iteration_summary_request(message)
        for message in cleaned
    )
    assert streaming._session_lacks_final_assistant_answer(cleaned) is True


def test_display_merge_does_not_render_synthetic_summary_prompt():
    previous_display = [{"role": "user", "content": "Do the long task."}]
    previous_context = [{"role": "user", "content": "Do the long task."}]
    result_messages = previous_context + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    result_messages = streaming._drop_synthetic_max_iteration_summary_requests(
        result_messages,
        enabled=True,
    )

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Do the long task.",
    )

    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in merged
    )
    assert merged[-1]["role"] == "assistant"
    assert "here is the summary" in merged[-1]["content"]


def test_frontend_handles_tool_limit_apperror_label():
    messages_js = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = messages_js.find("source.addEventListener('apperror'")
    end = messages_js.find("source.addEventListener('warning'", start)
    assert start != -1 and end != -1
    block = messages_js[start:end]

    assert "const isToolLimitReached=d.type==='tool_limit_reached';" in block
    assert "Tool iteration limit reached" in block
    assert "Terminal state details" in block


def test_streaming_tool_limit_with_final_answer_persists_clean_done_state(tmp_path, monkeypatch):
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["terminal_reason"] == "max_iterations"
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["context_messages"]
    )
    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"


def test_streaming_tool_limit_partial_without_final_answer_emits_no_final_apperror(tmp_path, monkeypatch):
    result = {
        "status": "partial",
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected apperror SSE payload"
    assert apperror_payloads[-1]["type"] == "tool_limit_reached"
    assert apperror_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert payload["messages"][-1]["_error"] is True
    assert "Tool iteration limit reached" in payload["messages"][-1]["content"]
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )


def test_streaming_tool_limit_with_fallback_final_response_surfaces_closure_text(tmp_path, monkeypatch):
    """#5494 — handle_max_iterations() guarantees a non-empty ``final_response``
    on iteration-limit exhaustion. This test pins the WebUI contract that,
    when ``messages`` ends without a final assistant turn and ``final_response``
    is set, the user sees that closure text instead of a bare
    ``tool_limit_reached`` error. Serves both as a live-bug fix pin and as a
    regression guard for the agent's "delivered final_response ⇒ assistant
    row" invariant: if a future agent regression drops that invariant, this
    test still passes because the WebUI honors the contract locally.
    """
    graceful = "I reached the iteration limit and couldn't generate a summary."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "final_response": graceful,
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    # The user sees the graceful fallback, not a bare tool_limit_reached error.
    # Either (a) we synthesized the fallback here, or (b) the agent guarantee
    # already added an assistant row and `_mark_latest_assistant_tool_limit_status`
    # attached the status card. Both routes satisfy the contract.
    assert not [
        ap for ev, ap in events if ev == "apperror"
        and ap.get("type") == "tool_limit_reached"
    ], "expected no tool_limit_reached apperror when fallback was returned"
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"

    # Fallback text is shown as a final assistant message and is annotated
    # with the status card so the UI can render the 'limit reached' chip.
    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == graceful
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"
    # Synthetic scaffolding turn was still dropped, even after fallback injection.
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )


def test_streaming_tool_limit_with_fallback_does_not_double_inject_when_assistant_exists(tmp_path, monkeypatch):
    """#5494 — when the agent already appended a model-generated summary
    AND ``final_response`` carries the same text, the WebUI must not duplicate
    the assistant turn. Pins the no-op contract on the synthesis path.
    """
    summary = "I reached the limit; here is the summary."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "final_response": summary,
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": summary},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads
    assistant_msgs = [
        m for m in payload["messages"]
        if m.get("role") == "assistant" and m.get("content") == summary
    ]
    assert len(assistant_msgs) == 1, "fallback must not duplicate the existing summary"
    assert assistant_msgs[0]["_terminal_state"] == "tool_limit_reached"


def test_maybe_inject_max_iteration_summary_fallback_unit():
    """Unit-level coverage for the injection helper."""
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]
    graceful = "I reached the iteration limit and couldn't generate a summary."
    result = {"final_response": graceful}

    injected = streaming._maybe_inject_max_iteration_summary_fallback(messages, result)

    assert injected[-1]["role"] == "assistant"
    assert injected[-1]["content"] == graceful
    assert injected[-1]["_max_iteration_summary_fallback"] is True


def test_maybe_inject_max_iteration_summary_fallback_skips_when_assistant_present():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "real summary"},
    ]
    result = {"final_response": "fallback text"}

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, result)
    assert out == messages


def test_maybe_inject_max_iteration_summary_fallback_skips_when_no_fallback():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
    ]

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, {})
    assert out == messages

    out = streaming._maybe_inject_max_iteration_summary_fallback(
        messages, {"final_response": "   "}
    )
    assert out == messages

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, None)
    assert out == messages


def test_streaming_tool_limit_partial_with_final_answer_suppresses_false_no_response(
    tmp_path,
    monkeypatch,
):
    result = {
        "status": "partial",
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    assert not [event_payload for event, event_payload in events if event == "apperror"]
    done_payloads = [event_payload for event, event_payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["terminal_reason"] == "max_iterations"
    assistant = next(
        message
        for message in payload["messages"]
        if message.get("role") == "assistant"
        and message.get("content") == "I reached the limit; here is the summary."
    )
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_terminal_reason"] == "max_iterations"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"
    assert payload["messages"][-1] is assistant


def test_streaming_historical_synthetic_prompt_normal_result_does_not_emit_tool_limit(tmp_path, monkeypatch):
    result = {
        "messages": [
            {"role": "user", "content": "Earlier task."},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "Current task completed normally."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    assert not [payload for event, payload in events if event == "apperror"]
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected normal done SSE payload"
    assert "terminal_state" not in done_payloads[-1]
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["content"] == "Current task completed normally."


def test_streaming_empty_result_messages_do_not_treat_prior_assistant_as_current_answer(tmp_path, monkeypatch):
    prior = [
        {"role": "user", "content": "Earlier task."},
        {"role": "assistant", "content": "Earlier answer."},
    ]
    result = {"messages": []}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior,
    )

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected silent-failure apperror"
    assert apperror_payloads[-1]["type"] == "no_response"
    assert not [payload for event, payload in events if event == "done"]
    assert payload["messages"][-1]["_error"] is True
