"""Regression test — surface the session-turn-lease wait instead of a dead screen.

Symptom (2026-08-17, sessions 20260817_080831_2bc2b0 / 20260817_082634_87cafb):
a user replied inside a mid-turn compaction *continuation* session while the
parent turn was still running. The cross-process turn lease is scoped to the
conversation lineage, so the new turn correctly waited for the running one.

The Agent does emit progress while it waits (run_agent.py `_on_session_turn_lease_wait`
-> `_emit_status`), and it emits a terminal notice on timeout via `_emit_warning`:

    "⏳ Another Hermes process is using this session; waiting for it to finish..."
    "⏳ Still waiting for the other Hermes process on this session (Ns)..."
    "⏳ Another Hermes process kept this session busy too long. ..."

But `_agent_status_callback` in api/streaming.py forwarded only compression and
fallback lifecycle messages and dropped everything else ("All other lifecycle
messages are dropped"). The browser therefore showed nothing at all for 30
minutes until the lease wait hit its 1800s ceiling and the turn failed with
`session_turn_lease_timeout`.

This is a *diagnostic* fix only: the lease semantics are correct and untouched.
The wait must simply become visible.

Layers covered:
  1. behavioral — the classifier helper recognises lease-wait lifecycle messages
     and ignores unrelated ones;
  2. wiring — the bridge forwards them as a 'warning' SSE event, which the
     existing messages.js 'warning' listener renders via setComposerStatus.
"""
from pathlib import Path

from api import streaming

ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

# Exact shapes emitted by hermes-agent run_agent.py (_on_session_turn_lease_wait
# and the lease-timeout branch).
_WAIT_FIRST = (
    "\u23f3 Another Hermes process is using this session; "
    "waiting for it to finish before starting your turn..."
)
_WAIT_AGAIN = (
    "\u23f3 Still waiting for the other Hermes process on this session (312s)..."
)
_WAIT_TIMEOUT = (
    "\u23f3 Another Hermes process kept this session busy too long. Your message "
    "was not processed - wait for the other process to finish, then send it again."
)
_LEASE_FREED = "Session is free; loading the latest transcript..."


def test_helper_recognises_lease_wait_messages():
    """Every lease-wait lifecycle shape the Agent emits must be recognised."""
    assert streaming._is_session_lease_wait_message("lifecycle", _WAIT_FIRST)
    assert streaming._is_session_lease_wait_message("lifecycle", _WAIT_AGAIN)
    assert streaming._is_session_lease_wait_message("warn", _WAIT_TIMEOUT)
    assert streaming._is_session_lease_wait_message("lifecycle", _LEASE_FREED)


def test_helper_ignores_unrelated_lifecycle_messages():
    """Unrelated lifecycle chatter must NOT be promoted to a user-visible warning."""
    assert not streaming._is_session_lease_wait_message("lifecycle", "")
    assert not streaming._is_session_lease_wait_message(
        "lifecycle", "Compressing conversation history..."
    )
    assert not streaming._is_session_lease_wait_message(
        "lifecycle", "\u274c Non-retryable error (HTTP 400): bad model"
    )
    # A *different* process-busy phrasing that is not the lease wait.
    assert not streaming._is_session_lease_wait_message(
        "lifecycle", "Another tool is running"
    )


def test_lease_wait_is_forwarded_to_sse_as_warning():
    """The bridge must forward lease-wait status as a 'warning' SSE event.

    messages.js already listens for 'warning' and renders d.message through
    setComposerStatus(); a non-'fallback' type yields a persistent notice,
    which is what a multi-minute wait needs.
    """
    assert "_is_session_lease_wait_message(_kind, _message)" in STREAMING_PY, (
        "the bridge must consult the lease-wait helper"
    )
    assert "'type': 'session_lease_wait'" in STREAMING_PY, (
        "lease-wait warnings need their own type so the frontend can style/persist them"
    )


def test_lease_wait_check_runs_before_the_generic_drop():
    """The check must sit inside _agent_status_callback, before the fallback
    branch returns — otherwise the message is dropped as before."""
    start = STREAMING_PY.index("def _agent_status_callback(")
    end = STREAMING_PY.index("# xsession wakeup misroute root fix", start)
    body = STREAMING_PY[start:end]
    assert "_is_session_lease_wait_message" in body, (
        "the lease-wait bridge must live inside _agent_status_callback"
    )
    assert body.index("_is_session_lease_wait_message") < body.index(
        "_is_fallback_lifecycle_message"
    ), "lease-wait must be handled before the fallback branch"


def test_docstring_no_longer_claims_all_other_messages_are_dropped():
    """The bridge docstring must not still claim every other lifecycle message
    is dropped — that stale claim is what hid this bug."""
    start = STREAMING_PY.index("def _agent_status_callback(")
    end = STREAMING_PY.index("# xsession wakeup misroute root fix", start)
    body = STREAMING_PY[start:end]
    assert "All other lifecycle messages are dropped." not in body
