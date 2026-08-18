"""A mid-turn compression hop must keep the parent's live stream visible.

Observed failure (PWA ``/session/20260818_160843_65a13b`` while the turn was
still running under parent ``20260818_144941_efbd27``, 2026-08-18):

  * mid-turn compression rotated the URL to the continuation hop;
  * the hop has no WebUI sidecar yet (that write happens at end-of-turn);
  * GET /api/session synthesizes an idle state.db stitch: no active_stream_id,
    no is_streaming, no runtime journal, often no parent_session_id;
  * loadSession therefore takes the idle path, paints a finished transcript,
    and never attaches the still-live SSE — tool cards vanish.

The previous rolling-identity fix (``_adoptRotatedStreamSession``) only helps
when the same EventSource stays open. A PWA reload, a deep link, or a second
tab opening the hop URL still sees an idle session.

Contract: a compression continuation whose origin still owns a live run must
project that run onto the hop (stream id + origin sid) so reload/attach can
rejoin the same SSE and accept events still emitted under the origin id.
A user fork (parent_session_id without a compression end_reason) must not
inherit a parent's live run.
"""

from __future__ import annotations

import re
from pathlib import Path

from api.session_live_stream import (
    apply_live_stream_lineage_projection,
    resolve_live_stream_for_session_lineage,
)

REPO = Path(__file__).resolve().parents[1]
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
CACHE_PY = (REPO / "api" / "route_session_list_cache.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


PARENT = "20260818_144941_efbd27"
HOP = "20260818_160843_65a13b"
FORK = "20260818_160000_fork01"
STREAM = "930868a7fd334d009560d4777b2dba8e"


def _rows():
    return {
        PARENT: {
            "id": PARENT,
            "session_id": PARENT,
            "parent_session_id": None,
            "end_reason": "compression",
            "ended_at": 1_000.0,
            "started_at": 100.0,
            "source": "cli",
        },
        HOP: {
            "id": HOP,
            "session_id": HOP,
            "parent_session_id": PARENT,
            "end_reason": None,
            "ended_at": None,
            "started_at": 1_001.0,
            "source": "cli",
        },
        FORK: {
            "id": FORK,
            "session_id": FORK,
            "parent_session_id": PARENT,
            "end_reason": None,
            "ended_at": None,
            "started_at": 1_001.0,
            "source": "cli",
            "session_source": "fork",
        },
    }


def _lookup(sid: str):
    return _rows().get(str(sid or "").strip())


def _active(sid: str):
    return STREAM if sid == PARENT else None


def test_compression_hop_inherits_parent_live_stream():
    got = resolve_live_stream_for_session_lineage(
        HOP,
        lineage_lookup=_lookup,
        active_stream_for_session=_active,
    )
    assert got == {
        "stream_id": STREAM,
        "origin_session_id": PARENT,
    }


def test_session_with_its_own_live_run_is_not_rewritten():
    got = resolve_live_stream_for_session_lineage(
        PARENT,
        lineage_lookup=_lookup,
        active_stream_for_session=_active,
    )
    assert got == {
        "stream_id": STREAM,
        "origin_session_id": PARENT,
    }


def test_user_fork_does_not_inherit_parent_live_stream():
    got = resolve_live_stream_for_session_lineage(
        FORK,
        lineage_lookup=_lookup,
        active_stream_for_session=_active,
    )
    assert got is None


def test_unknown_session_has_no_live_stream():
    got = resolve_live_stream_for_session_lineage(
        "missing",
        lineage_lookup=_lookup,
        active_stream_for_session=_active,
    )
    assert got is None


def test_synthesized_hop_payload_projects_parent_runtime():
    payload = {
        "session_id": HOP,
        "title": "live turn",
        "messages": [],
        "tool_calls": [],
        "is_cli_session": True,
    }
    out = apply_live_stream_lineage_projection(
        payload,
        lineage_lookup=_lookup,
        active_stream_for_session=_active,
    )
    assert out["active_stream_id"] == STREAM
    assert out["is_streaming"] is True
    assert out["stream_origin_session_id"] == PARENT
    assert out["parent_session_id"] == PARENT


def test_cli_close_parent_does_not_inherit_live_stream():
    rows = _rows()
    rows[PARENT]["end_reason"] = "cli_close"
    got = resolve_live_stream_for_session_lineage(
        HOP,
        lineage_lookup=rows.get,
        active_stream_for_session=_active,
    )
    assert got is None


def test_get_session_synth_path_projects_live_lineage_stream():
    """A sidecar-less hop must not be returned as an idle stitch."""
    start = ROUTES_PY.index("No WebUI sidecar. Delegate to the shared foreign-session")
    body = ROUTES_PY[start:start + 8000]
    assert "apply_live_stream_lineage_projection" in body, (
        "GET /api/session synthesis must project the origin run onto a "
        "sidecar-less compression hop, otherwise a PWA reload of the hop URL "
        "paints a finished transcript and never attaches the live SSE"
    )
    assert "stream_origin_session_id" in body or "runtime_journal_snapshot" in body


def test_sidebar_runtime_overlay_projects_live_lineage_stream():
    assert "overlay_live_stream_lineage_on_session_rows" in CACHE_PY, (
        "the cached /api/sessions runtime overlay must project a live origin "
        "stream onto idle compression hops, or the sidebar shows the hop as "
        "stopped while the parent is still running"
    )


def test_attach_live_stream_seeds_origin_session_into_owned_sids():
    start = MESSAGES_JS.index("\nfunction attachLiveStream(")
    end = MESSAGES_JS.index("\nfunction ", start + 1)
    body = MESSAGES_JS[start:end]
    assert "stream_origin_session_id" in body, (
        "attachLiveStream must seed _streamOwnedSids with the origin session "
        "id. The server keeps emitting this turn's tool events under the "
        "pre-rotation parent id; without that seed a hop reload attaches "
        "the right stream then drops every tool card"
    )


def test_load_session_passes_projected_stream_to_attach():
    """loadSession already attaches when active_stream_id is set; keep that."""
    assert re.search(
        r"let\s+activeStreamId\s*=\s*S\.session\.active_stream_id",
        SESSIONS_JS,
    )
