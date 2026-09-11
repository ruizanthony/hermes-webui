"""A mid-turn compression rotation must not silence the live stream UI.

Observed failure (session 4169a7cacdd3 -> 20260817_142640_dada12, 2026-08-17
14:26 UTC): a long turn compressed mid-flight. The rotation itself worked --
``midturn_compacted`` was emitted with the correct origin/continuation pair and
the browser followed it by rewriting ``S.session.session_id`` to the
continuation id. From that instant the conversation LOOKED frozen:

  * no spinning activity badge next to the sidebar title;
  * Transparent Stream showed interim commentary but NO tool cards between
    them, and no "running" row;
  * the turn nevertheless kept producing events for ~9 more minutes
    (28 ``tool`` + 28 ``tool_complete`` + 71 ``token`` events observed in
    ``_run_journal/4169a7cacdd3/<run>.jsonl`` AFTER the rotation timestamp).

Root cause -- a stale identity captured by closure
--------------------------------------------------
``attachLiveStream(activeSid, streamId, ...)`` captures ``activeSid`` once, at
attach time, and every SSE handler installed by its inner ``_wireSSE`` guards
on it:

    if(!S.session||S.session.session_id!==activeSid||...) return;

The ``midturn_compacted`` and ``compressed`` handlers deliberately reassign
``S.session.session_id`` to the continuation id (so the address bar and any
reload follow the rotation instead of resurrecting the frozen pre-compression
archive). Nothing reassigns ``activeSid``.

So after the first mid-turn rotation the two identities disagree forever:
``S.session.session_id`` is the continuation, ``activeSid`` is the pre-rotation
id, and every guard of that shape evaluates false for the rest of the turn.
The events keep arriving and keep mutating the accumulators -- which is why
``interim_assistant`` prose (whose early-return happens AFTER
``assistantText`` is updated) still reached the DOM on the next matching
render, while ``tool`` / ``tool_complete`` (which return BEFORE touching the
DOM) silently dropped their cards.

The server-side truth is unaffected: the run journal keeps writing under the
ORIGIN session id for the whole turn, so this is purely a client identity bug.

Fix contract asserted here
--------------------------
1. The live-stream identity must be re-resolvable, not frozen: a rotation
   handler has to move the stream's own notion of "the session this stream
   belongs to" to the continuation id, and the SSE guards must consult that
   rolling value rather than the attach-time constant.
2. Sidebar streaming state must be carried across the rotation, so the
   continuation row shows the running badge instead of appearing idle.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _attach_live_stream_body() -> str:
    """Source of ``attachLiveStream`` up to the next top-level function."""
    start = MESSAGES_JS.index("\nfunction attachLiveStream(")
    end = MESSAGES_JS.index("\nfunction ", start + 1)
    return MESSAGES_JS[start:end]


def _handler_body(name: str) -> str:
    """Source of a ``source.addEventListener('<name>',e=>{...})`` handler."""
    m = re.search(
        rf"source\.addEventListener\('{re.escape(name)}',e=>\{{(.*?)\n    \}}\);",
        MESSAGES_JS,
        re.S,
    )
    assert m, f"could not isolate the {name!r} SSE handler"
    return m.group(1)


def test_rotation_handlers_advance_the_live_stream_session_identity():
    """``midturn_compacted`` / ``compressed`` rewrite ``S.session.session_id``;
    they must advance the stream's own identity in the same breath, otherwise
    every ``session_id!==activeSid`` guard in the same closure goes false for
    the rest of the turn."""
    for handler in ("midturn_compacted", "compressed"):
        body = _handler_body(handler)
        # The handler is the one that performs the rotation.
        assert "S.session.session_id=continuationSid" in body, (
            f"{handler} no longer rotates S.session.session_id; this test "
            "needs updating"
        )
        assert "_adoptRotatedStreamSession" in body, (
            f"the {handler} handler rotates S.session.session_id to the "
            "continuation but never advances the live stream's own session "
            "identity. Every SSE guard of the form "
            "'S.session.session_id!==activeSid' in the same closure will be "
            "false for the rest of the turn: tool cards, the running row and "
            "the sidebar badge all go silent while the turn keeps running."
        )


def test_sse_guards_resolve_a_rotatable_identity_not_a_frozen_constant():
    """The tool/tool_complete guards must consult the rolling identity.

    The fix keeps the guards spelled ``S.session.session_id!==activeSid`` but
    makes ``activeSid`` itself a mutable ``let`` that the rotation chokepoint
    reassigns -- fixing all ~28 guards at once instead of editing each one.
    What matters is that the compared value is re-resolvable, so this asserts
    the binding is mutable and rotated, not the guard's spelling.
    """
    body = _attach_live_stream_body()
    assert not re.search(r"function attachLiveStream\(activeSid\b", body), (
        "attachLiveStream still binds 'activeSid' directly as its (immutable "
        "by contract) parameter. A mid-turn rotation cannot advance it, so "
        "every guard comparing against it goes stale for the rest of the turn."
    )
    assert re.search(r"let\s+activeSid\s*=\s*attachSid\b", body), (
        "the live-stream session identity must be a mutable binding seeded "
        "from the attach-time argument, so the rotation chokepoint can "
        "advance it"
    )
    for handler in ("tool", "tool_complete"):
        hbody = _handler_body(handler)
        assert "S.session.session_id!==activeSid" in hbody, (
            f"the {handler!r} handler no longer guards on the live identity; "
            "this test needs updating"
        )


def test_rolling_identity_is_declared_and_seeded_from_the_attach_argument():
    body = _attach_live_stream_body()
    assert re.search(r"let\s+activeSid\s*=\s*attachSid\b", body), (
        "attachLiveStream must declare a mutable session identity, seeded "
        "from the attach-time argument, as the single rolling value the "
        "SSE handlers consult"
    )
    assert "function _adoptRotatedStreamSession(" in body, (
        "attachLiveStream must expose a single chokepoint that adopts a "
        "rotated session id (identity + per-session bookkeeping), instead of "
        "each rotation handler patching state ad hoc"
    )


def test_rotation_chokepoint_migrates_per_session_stream_bookkeeping():
    """INFLIGHT / LIVE_STREAMS and friends are keyed by session id. If the
    identity moves but the maps do not, the continuation has no inflight entry
    and reconnect/restore paths cannot find the live turn."""
    body = _attach_live_stream_body()
    m = re.search(
        r"function _adoptRotatedStreamSession\((.*?)\n  \}",
        body,
        re.S,
    )
    assert m, "could not isolate _adoptRotatedStreamSession"
    fn = m.group(1)
    for keyed in ("INFLIGHT", "LIVE_STREAMS"):
        assert keyed in fn, (
            f"_adoptRotatedStreamSession must migrate the {keyed} entry to "
            "the continuation id; leaving it under the pre-rotation key "
            "orphans the live turn state"
        )
    assert re.search(r"\bactiveSid\s*=\s*next\b", fn), (
        "_adoptRotatedStreamSession must advance the rolling identity "
        "(reassign the mutable session id to the continuation)"
    )


def test_sidebar_streaming_state_follows_the_rotation():
    """The continuation session row must inherit the running indicator, or the
    sidebar shows a conversation that looks stopped while it is still working."""
    assert "function _adoptSessionStreamingRotation(" in SESSIONS_JS, (
        "sessions.js must expose a helper that carries sidebar streaming "
        "state (the spinning badge) from the pre-rotation session id to the "
        "continuation id"
    )
    m = re.search(
        r"function _adoptSessionStreamingRotation\((.*?)\n\}",
        SESSIONS_JS,
        re.S,
    )
    assert m, "could not isolate _adoptSessionStreamingRotation"
    fn = m.group(1)
    assert "_sessionStreamingById" in fn, (
        "the rotation helper must move the _sessionStreamingById entry so the "
        "continuation row renders as streaming"
    )
    assert "is_streaming" in fn, (
        "the rotation helper must mark the continuation row is_streaming so a "
        "cache repaint keeps the badge"
    )
