"""Structural guards for quiet background-completion live-view events.

A ``bg_task_complete`` event wakes the agent server-side. It is transport and
bookkeeping data, not a user-facing status. The handler must dedupe and ack the
event without showing process ids or technical summaries; the agent's eventual
assistant response is the only communication surface.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _handler_body() -> str:
    js = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = js.index("function _handleBgTaskCompleteEvent(")
    end = js.index("\nfunction ", start + 1)
    return js[start:end]


def test_completion_event_dedupes_before_viewed_bookkeeping():
    body = _handler_body()
    dedupe_idx = body.index("_bgTaskCompleteRingBufferAdd(sid, evt_id)")
    viewed_idx = body.index("_isSessionActivelyViewed(sid)")

    assert dedupe_idx < viewed_idx
    assert "_markSessionViewed" in body
    assert "_clearSessionCompletionUnread" in body


def test_completion_event_never_shows_raw_process_toast():
    body = _handler_body()

    assert "showToast(" not in body
    assert "Task ${tid} done" not in body
    assert "d.summary" not in body


def test_diagnostic_ack_remains_after_bookkeeping():
    body = _handler_body()

    assert body.index("_isSessionActivelyViewed(sid)") < body.index("api/bg-task-complete-ack")
    assert "task_id: pid" in body
