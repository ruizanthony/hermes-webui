"""Project a live run onto a mid-turn compression continuation hop.

A long turn can rotate the conversation id (and the PWA URL) before the
WebUI sidecar for the continuation exists. GET /api/session then synthesizes
an idle stitch and the tab looks finished while the origin run is still
emitting SSE under the parent id. This module is the shared resolver for
that class: walk compression parents, never user forks.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Callable

from api.agent_sessions import _is_continuation_session, _optional_col, open_state_db_readonly

_MAX_LINEAGE_HOPS = 8

LineageLookup = Callable[[str], dict | None]
ActiveStreamLookup = Callable[[str], str | None]


def _normalize_sid(session_id: str | None) -> str:
    return str(session_id or "").strip()


def _is_compression_live_continuation(parent: dict | None, child: dict | None) -> bool:
    if not _is_continuation_session(parent, child):
        return False
    return str((parent or {}).get("end_reason") or "").strip() == "compression"


def live_stream_ids_by_session() -> dict[str, str]:
    """Return session_id -> live stream_id for runs still in STREAMS."""
    mapping: dict[str, str] = {}
    try:
        from api import config as cfg
    except Exception:
        return mapping
    try:
        with cfg.STREAMS_LOCK:
            live_ids = {str(stream_id) for stream_id in (cfg.STREAMS or {})}
        with cfg.ACTIVE_RUNS_LOCK:
            runs = list((cfg.ACTIVE_RUNS or {}).items())
    except Exception:
        return mapping
    for run_stream_id, raw in runs:
        stream_id = str((raw or {}).get("stream_id") or run_stream_id or "").strip()
        run_sid = str((raw or {}).get("session_id") or "").strip()
        if not run_sid or not stream_id:
            continue
        if stream_id in live_ids or str(run_stream_id) in live_ids:
            mapping[run_sid] = stream_id
    return mapping


def _default_active_stream_for_session(session_id: str) -> str | None:
    sid = _normalize_sid(session_id)
    if not sid:
        return None
    return live_stream_ids_by_session().get(sid)


def _read_state_db_session_rows(
    session_ids: set[str],
    *,
    profile=None,
) -> dict[str, dict]:
    wanted = {_normalize_sid(sid) for sid in session_ids if _normalize_sid(sid)}
    if not wanted:
        return {}
    try:
        from api.models import _agent_state_db_path

        db_path = _agent_state_db_path(profile=profile)
    except Exception:
        return {}
    if db_path is None:
        return {}
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        import sqlite3
    except ImportError:
        return {}
    try:
        with closing(open_state_db_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            columns = {row[1] for row in cur.fetchall()}
            if "id" not in columns:
                return {}
            parent_expr = _optional_col("parent_session_id", columns)
            end_reason_expr = _optional_col("end_reason", columns)
            ended_expr = _optional_col("ended_at", columns)
            started_expr = _optional_col("started_at", columns, "0")
            source_expr = _optional_col("source", columns)
            session_source_expr = _optional_col("session_source", columns)
            placeholders = ",".join("?" for _ in wanted)
            cur.execute(
                f"""
                SELECT s.id,
                       {parent_expr},
                       {end_reason_expr},
                       {ended_expr},
                       {started_expr},
                       {source_expr},
                       {session_source_expr}
                FROM sessions s
                WHERE s.id IN ({placeholders})
                """,
                tuple(wanted),
            )
            rows: dict[str, dict] = {}
            for raw in cur.fetchall():
                row = dict(raw)
                sid = _normalize_sid(row.get("id"))
                if not sid:
                    continue
                row["session_id"] = sid
                rows[sid] = row
            return rows
    except Exception:
        return {}


def _default_lineage_lookup(session_id: str) -> dict | None:
    sid = _normalize_sid(session_id)
    if not sid:
        return None
    return _read_state_db_session_rows({sid}).get(sid)


def resolve_live_stream_for_session_lineage(
    session_id: str | None,
    *,
    lineage_lookup: LineageLookup | None = None,
    active_stream_for_session: ActiveStreamLookup | None = None,
) -> dict | None:
    """Return the live stream a session should rejoin, if any.

    Own run wins. Otherwise walk compression parents only (never forks /
    ``cli_close`` / unrelated ``parent_session_id``) until a live origin is
    found or the hop bound is hit.
    """
    sid = _normalize_sid(session_id)
    if not sid:
        return None
    lookup = lineage_lookup or _default_lineage_lookup
    active = active_stream_for_session or _default_active_stream_for_session

    own = active(sid)
    if own:
        return {"stream_id": str(own), "origin_session_id": sid}

    child_sid = sid
    try:
        child = lookup(child_sid)
    except Exception:
        return None
    if not child:
        return None
    seen = {child_sid}
    for _ in range(_MAX_LINEAGE_HOPS):
        parent_sid = _normalize_sid((child or {}).get("parent_session_id"))
        if not parent_sid or parent_sid in seen:
            return None
        try:
            parent = lookup(parent_sid)
        except Exception:
            return None
        if not parent:
            return None
        if not _is_compression_live_continuation(parent, child):
            return None
        stream_id = active(parent_sid)
        if stream_id:
            return {
                "stream_id": str(stream_id),
                "origin_session_id": parent_sid,
            }
        seen.add(parent_sid)
        child_sid = parent_sid
        child = parent
    return None


def apply_live_stream_lineage_projection(
    payload: dict,
    *,
    lineage_lookup: LineageLookup | None = None,
    active_stream_for_session: ActiveStreamLookup | None = None,
) -> dict:
    """Stamp live-stream fields onto a session GET/list payload in place."""
    if not isinstance(payload, dict):
        return payload
    sid = _normalize_sid(payload.get("session_id"))
    if not sid:
        return payload
    live = resolve_live_stream_for_session_lineage(
        sid,
        lineage_lookup=lineage_lookup,
        active_stream_for_session=active_stream_for_session,
    )
    if not live:
        return payload
    payload["active_stream_id"] = live["stream_id"]
    payload["is_streaming"] = True
    payload["stream_origin_session_id"] = live["origin_session_id"]
    if live["origin_session_id"] != sid and not payload.get("parent_session_id"):
        payload["parent_session_id"] = live["origin_session_id"]
    return payload


def overlay_live_stream_lineage_on_session_rows(rows: list[dict]) -> list[dict]:
    """Project live origin streams onto idle compression hops in a sidebar list.

    One batched state.db read covers the live origins plus their in-list
    children so a sidebar poll does not open the agent DB per row.
    """
    if not rows:
        return rows
    live = live_stream_ids_by_session()
    if not live:
        return rows
    needed: set[str] = set(live)
    candidates: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = _normalize_sid(row.get("session_id"))
        if not sid or row.get("active_stream_id"):
            continue
        parent_sid = _normalize_sid(row.get("parent_session_id"))
        if not parent_sid:
            continue
        candidates.append(row)
        needed.add(sid)
        needed.add(parent_sid)
    if not candidates:
        return rows
    table = _read_state_db_session_rows(needed)

    def lookup(sid: str):
        return table.get(_normalize_sid(sid))

    def active(sid: str):
        return live.get(_normalize_sid(sid))

    for row in candidates:
        apply_live_stream_lineage_projection(
            row,
            lineage_lookup=lookup,
            active_stream_for_session=active,
        )
    return rows
