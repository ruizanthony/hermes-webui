"""Regression: a state.db-only row must never render after a newer reply.

Real-world shape (session 20260822_084749_7d1e2c, reported 2026-08-23):

A user prompt exists in state.db with a UNIQUE typed row id that the sidecar
does not carry (the two stores do not share an id space). That makes
``row_id_preserves_source_multiplicity`` true, which bypasses the
sidecar-timestamp-range block in ``merge_session_messages_append_only`` --
including the ``_insert_state_message_chronologically`` call that block owns --
and drops the row onto the terminal ``merged_messages.append``.

The row therefore renders LAST, underneath the assistant turn that answered it.
The full display path hides this because it re-sorts by timestamp afterwards;
the paginated path (``msg_limit``, used for the initial render) does not, so the
user sees their own message below its own answer.

The merge itself must produce a chronologically coherent transcript.
"""

from api.models import merge_session_messages_append_only


def _user(content, ts, **extra):
    return {"role": "user", "content": content, "timestamp": ts, **extra}


def _assistant(content, ts, **extra):
    return {"role": "assistant", "content": content, "timestamp": ts, **extra}


def test_unique_state_row_id_user_row_keeps_chronological_slot():
    """A state-only user row must land before the reply it triggered."""
    # Sidecar: the settled transcript. Its rows carry ids from the sidecar's
    # own numbering space.
    sidecar = [
        _user("question un", 1000.0, id=1),
        _assistant("reponse un", 1010.0, id=2),
        _assistant("conclusion finale", 1200.0, id=3),
    ]

    # state.db: the same conversation, but this prompt was persisted only here,
    # under a typed row id that is unique to state.db and absent from the
    # sidecar. Its timestamp sits BEFORE the settled conclusion above.
    state = [
        _user("question deux", 1100.0, id=848467),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    contents = [m.get("content") for m in merged]
    assert "question deux" in contents, (
        "the state-only user row must be preserved (append-only contract)"
    )

    # The core invariant: the recovered prompt must not sit after the newer
    # assistant turn.
    assert contents.index("question deux") < contents.index("conclusion finale"), (
        "state-only user row rendered AFTER a newer assistant reply: "
        f"{contents}"
    )

    # And the transcript as a whole must be non-decreasing in time.
    timestamps = [m.get("timestamp") for m in merged]
    assert timestamps == sorted(timestamps), (
        f"merged transcript is not chronologically ordered: {timestamps}"
    )


def test_unique_state_row_id_row_still_appends_when_newest():
    """A genuinely newer state-only row still belongs at the tail."""
    sidecar = [
        _user("question un", 1000.0, id=1),
        _assistant("reponse un", 1010.0, id=2),
    ]
    state = [
        _user("question tardive", 2000.0, id=848468),
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [m.get("content") for m in merged][-1] == "question tardive"
