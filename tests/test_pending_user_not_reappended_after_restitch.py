"""Do not re-append a current-turn prompt that is already in the transcript.

Observed (WebUI): after a long turn that restitches earlier history *after*
the already-checkpointed user row, the last user is no longer the active
prompt. Recovery / end-of-turn materialization then appends a second copy of
the same prompt at the tail — so the conclusion renders above the user's
question.

Tail-only matching is the fault. A previous turn with identical text must
still materialize a new row; a restitch after the current checkpoint must not.
"""

from __future__ import annotations

from api.models import _append_recovered_pending_turn
from api.streaming import (
    _materialize_pending_user_turn_before_error,
    _turn_transcript_lacks_final_assistant_answer,
)

PROMPT = "Finalise les éléments restant à faire de cette conversation"
STARTED = 1787079240.0


class _Session:
    def __init__(self, messages, pending=PROMPT, started=STARTED):
        self.messages = [dict(m) for m in messages]
        self.context_messages = [dict(m) for m in messages]
        self.pending_user_message = pending
        self.pending_started_at = started
        self.pending_user_source = "webui"
        self.pending_attachments = []
        self.truncation_watermark = None
        self.session_id = "test-restitch-pending"
        self.active_stream_id = "stream-test"
        self.path = ""

    def save(self, *args, **kwargs):
        return None


def _restitched_completed_transcript():
    return [
        {"role": "user", "content": "older question", "timestamp": STARTED - 3600, "id": 1},
        {"role": "assistant", "content": "older answer", "timestamp": STARTED - 3500, "id": 2},
        {"role": "user", "content": PROMPT, "timestamp": STARTED, "id": 213},
        {"role": "user", "content": "older question", "timestamp": STARTED - 3600, "id": 1},
        {"role": "assistant", "content": "older answer", "timestamp": STARTED - 3500, "id": 2},
        {"role": "assistant", "content": "Les deux points restants sont faits.", "timestamp": STARTED + 1800, "id": 900},
    ]


def test_append_recovered_pending_does_not_duplicate_after_history_restitch():
    session = _Session(_restitched_completed_transcript())
    before = len(session.messages)

    appended = _append_recovered_pending_turn(session, timestamp=int(STARTED))

    assert appended is None
    assert len(session.messages) == before
    assert session.messages[-1]["role"] == "assistant"
    assert [m.get("content") for m in session.messages if m.get("role") == "user"].count(PROMPT) == 1


def test_append_recovered_pending_still_materializes_repeated_prompt_on_new_turn():
    previous_same_text = [
        {"role": "user", "content": PROMPT, "timestamp": STARTED - 3600, "id": 10},
        {"role": "assistant", "content": "already done once", "timestamp": STARTED - 3500, "id": 11},
    ]
    session = _Session(previous_same_text)

    appended = _append_recovered_pending_turn(session, timestamp=int(STARTED))

    assert appended is not None
    assert session.messages[-1]["role"] == "user"
    assert session.messages[-1]["content"] == PROMPT
    assert [m.get("content") for m in session.messages if m.get("role") == "user"].count(PROMPT) == 2


def test_error_materializer_does_not_append_when_current_prompt_already_exists():
    session = _Session(_restitched_completed_transcript())
    before = len(session.messages)

    appended = _materialize_pending_user_turn_before_error(session)

    assert appended is False
    assert len(session.messages) == before
    assert session.messages[-1]["role"] == "assistant"


def test_turn_evaluator_does_not_park_prompt_after_its_own_answer():
    previous_display = _restitched_completed_transcript()[:3]
    merged = _restitched_completed_transcript()

    lacks = _turn_transcript_lacks_final_assistant_answer(
        merged,
        previous_display,
        PROMPT,
        source="webui",
        drop_replayed_assistant=False,
    )

    assert lacks is False
    assert merged[-1]["role"] == "assistant"
    assert [m.get("content") for m in merged if m.get("role") == "user"].count(PROMPT) == 1
