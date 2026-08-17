"""A long turn can compress MORE THAN ONCE before it returns.

Observed failure (session 20260817_074600_1bffd4, 2026-08-17 07:55 UTC): a
single long turn triggered context compression twice:

    07:45:19  turn starts on session 2bdea5
    07:46:00  compression #1 rotates 2bdea5 -> 1bffd4 (rotated_committed)
    07:55:56  compression #2 rotates 1bffd4 -> 5b1a02 (rotated_committed)

``_agent_status_callback`` bridges each ``"compacted"`` status edge to a
``midturn_compacted`` SSE event via ``_emit_midturn_compaction_event(put,
origin_session_id=session_id, ...)``. ``session_id`` is the *original*
``_run_agent_streaming`` parameter captured by closure and never reassigned,
so BOTH emissions carried ``origin_session_id="...2bdea5"`` — the second one
wrongly repeating the first hop's origin instead of the intermediate hop
(1bffd4) the browser had already rotated to.

Client-side, ``static/messages.js``'s ``midturn_compacted`` listener drops
any event whose origin AND continuation both mismatch the tab's current
session id (a legitimate safety guard). After event #1 the tab was already on
1bffd4; event #2 (origin=2bdea5, continuation=5b1a02) matched neither, so it
was silently dropped. The tab was permanently stranded on 1bffd4 -- an
intermediate hop id that never receives its own WebUI sidecar (only the
turn's original and final ids get one via the end-of-turn reconciliation
block), so every later load of that URL synthesizes a giant unbounded
state.db stitch (8381 messages observed) instead of showing the live
conversation.

Fix: the callback must track a rolling "last known mid-turn id" that starts
at ``session_id`` and advances to the continuation only after a successful
emission, so each subsequent rotation's origin is accurate and the client's
existing (correct) matching guard follows every hop.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _nested_func_body(src: str, name: str) -> str:
    """Return the source of a nested ``def name(...):`` up to its dedent."""
    m = re.search(rf"\n(?P<indent>[ \t]+)def {re.escape(name)}\(", src)
    assert m, f"nested function {name} not found"
    indent = m.group("indent")
    start = m.start()
    body_start = src.index("\n", m.end()) + 1
    lines = src[body_start:].splitlines(keepends=True)
    end = body_start
    for line in lines:
        stripped = line.strip("\n")
        if stripped.strip() == "":
            end += len(line)
            continue
        this_indent = line[: len(line) - len(line.lstrip(" \t"))]
        if len(this_indent) <= len(indent):
            break
        end += len(line)
    return src[start:end]


def test_status_callback_does_not_reuse_the_stale_outer_session_id():
    """The mid-turn emitter must not hardcode the turn's ORIGINAL session id
    as the origin on every rotation -- a second rotation in the same turn
    needs the origin to be the id the browser is currently parked on (the
    first rotation's continuation), not the turn-start id.
    """
    body = _nested_func_body(STREAMING_PY, "_agent_status_callback")
    assert "_emit_midturn_compaction_event" in body
    bug_pattern = re.search(
        r"_emit_midturn_compaction_event\(\s*put,\s*origin_session_id=session_id,",
        body,
    )
    assert bug_pattern is None, (
        "_agent_status_callback still passes the closure-captured, "
        "never-updated 'session_id' parameter as origin_session_id on every "
        "mid-turn rotation. A turn that compresses twice will emit a stale "
        "origin on the second rotation, which the client's same-session "
        "match guard then silently drops -- stranding the tab on an "
        "intermediate hop id that never gets its own sidecar."
    )


def test_status_callback_advances_a_rolling_origin_after_each_rotation():
    """A rolling tracker variable must exist and be reassigned to the live
    continuation id only on a successful emission (fail-closed: a skipped
    emission -- e.g. durability check failed -- must not advance the
    pointer, so the next rotation still reports the correct un-shown origin).
    """
    body = _nested_func_body(STREAMING_PY, "_agent_status_callback")
    assert "nonlocal" in body, (
        "callback must declare a nonlocal rolling-origin tracker to survive "
        "across repeated invocations within the same turn"
    )
    # The rolling variable must be read as the origin AND conditionally
    # written back to the live continuation after a successful publish.
    assert re.search(r"origin_session_id=_midturn_\w+", body), (
        "origin_session_id must come from a rolling tracker variable, not "
        "the fixed outer 'session_id' parameter"
    )
