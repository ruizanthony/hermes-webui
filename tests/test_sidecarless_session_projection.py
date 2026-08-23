from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


pytestmark = pytest.mark.requires_agent_modules


def _make_continuation_db(path, *, parent_sid="projection_parent", child_sid="projection_child"):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            cwd TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            parent_session_id TEXT,
            message_count INTEGER,
            profile_name TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'webui', 'Parent', 'test-model', '/tmp', 1, 10, 'compression', NULL, 10, NULL)",
        (parent_sid,),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'webui', 'Child', 'test-model', '/tmp', 11, 20, NULL, ?, 10, NULL)",
        (child_sid, parent_sid),
    )
    for idx in range(20):
        sid = parent_sid if idx < 10 else child_sid
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if idx % 2 == 0 else "assistant", f"message-{idx}", float(idx)),
        )
    conn.commit()
    conn.close()
    return parent_sid, child_sid


def test_state_db_continuation_projection_supports_summary_and_absolute_before(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _parent, child = _make_continuation_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    summary = models.get_state_db_session_summary(child, stitch_continuations=True)
    page = models.get_state_db_session_messages(
        child,
        stitch_continuations=True,
        limit=5,
        before=12,
    )

    assert summary == {"message_count": 20, "last_message_at": 19.0}
    assert [row["content"] for row in page] == [
        "message-7",
        "message-8",
        "message-9",
        "message-10",
        "message-11",
    ]


def test_sidecarless_projection_grows_raw_slice_until_visible_budget(tmp_path, monkeypatch):
    import api.models as models
    import api.routes as routes

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, cwd TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT,
            parent_session_id TEXT, message_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL, active INTEGER DEFAULT 1
        );
        """
    )
    sid = "tool_heavy_projection"
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'tui', 'Tool heavy', 'test-model', '/tmp', 1, 100, NULL, NULL, 100)",
        (sid,),
    )
    for idx in range(100):
        role = "user" if idx < 10 and idx % 2 == 0 else (
            "assistant" if idx < 10 else "tool"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, role, f"message-{idx}", float(idx)),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    session, reason = routes._claim_or_synthesize_cli_session(
        sid,
        cli_meta=models.get_state_db_session_metadata(sid),
        message_limit=5,
    )

    assert reason == "materialized"
    assert session is not None
    assert [row["content"] for row in session.messages] == [
        f"message-{idx}" for idx in range(5, 10)
    ]
    assert getattr(session, "_projection_message_count") == 100
    assert getattr(session, "_projection_messages_offset") == 5


def test_claim_projection_before_zero_returns_an_empty_page(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    db_path = tmp_path / "state.db"
    _parent_sid, child_sid = _make_continuation_db(db_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    assert models.get_state_db_session_summary(
        child_sid, stitch_continuations=True
    )["message_count"] == 20

    synth, reason = routes._claim_or_synthesize_cli_session(
        child_sid,
        cli_meta={"source_tag": "tui"},
        message_limit=5,
        message_before=0,
    )

    assert reason == "materialized"
    assert synth is not None
    assert synth.messages == []
    assert getattr(synth, "_projection_message_count") == 20
    assert getattr(synth, "_projection_messages_offset") == 0


def _invoke_sidecarless_detail(monkeypatch, query: str, *, total=100, active_stream=False):
    import api.models as models
    import api.routes as routes
    import api.session_live_stream as live_stream

    sid = "projection_child"
    all_messages = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message-{idx}",
            "timestamp": float(idx),
        }
        for idx in range(total)
    ]
    captured = {"lookup_calls": 0, "snapshot_calls": 0}

    def missing_sidecar(_sid, metadata_only=False):
        raise KeyError(_sid)

    def forbidden_global_lookup(_sid, **_kwargs):
        captured["lookup_calls"] += 1
        raise AssertionError("state.db-backed detail must not scan every foreign session")

    def targeted_metadata(_sid, profile=None):
        assert _sid == sid
        return {
            "session_id": sid,
            "title": "Projected session",
            "workspace": "/tmp",
            "model": "test-model",
            "source_tag": "webui",
            "raw_source": "webui",
            "created_at": 1.0,
            "updated_at": 99.0,
            "last_message_at": 99.0,
            "profile": None,
        }

    def synthesize(_sid, *, cli_meta=None, message_limit=None, message_before=None):
        captured["message_limit"] = message_limit
        captured["message_before"] = message_before
        boundary = total if message_before is None else min(total, max(0, int(message_before)))
        if message_limit is None:
            page = list(all_messages)
            offset = 0
        elif int(message_limit) <= 0:
            page = []
            offset = 0
        else:
            offset = max(0, boundary - int(message_limit))
            page = all_messages[offset:boundary]
        session = models.Session(
            session_id=sid,
            title="Projected session",
            workspace="/tmp",
            model="test-model",
            messages=page,
            created_at=1.0,
            updated_at=99.0,
            is_cli_session=True,
            source_tag="webui",
            raw_source="webui",
            read_only=False,
        )
        session._projection_message_count = total
        session._projection_last_message_at = 99.0
        session._projection_messages_offset = offset
        return session, "materialized"

    def project_live(payload):
        if active_stream:
            payload["active_stream_id"] = "stream-sidecarless"

    def fake_j(_handler, payload, status=200, **_kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "get_session", missing_sidecar)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", forbidden_global_lookup)
    monkeypatch.setattr(routes, "get_state_db_session_metadata", targeted_metadata, raising=False)
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", synthesize)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(live_stream, "apply_live_stream_lineage_projection", project_live)
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: {"status": "running"})
    monkeypatch.setattr(routes, "_run_journal_status_payload", lambda *_args, **_kwargs: {"status": "running"})

    def snapshot(*_args, **_kwargs):
        captured["snapshot_calls"] += 1
        return {"messages": [{"role": "assistant", "content": "large snapshot"}]}

    monkeypatch.setattr(routes, "_run_journal_live_snapshot", snapshot)
    parsed = urlparse(f"/api/session?session_id={sid}&resolve_model=0&{query}")
    assert routes.handle_get(SimpleNamespace(), parsed) is True
    return captured


def test_sidecarless_metadata_only_skips_transcript_global_scan_and_runtime_snapshot(monkeypatch):
    captured = _invoke_sidecarless_detail(monkeypatch, "messages=0", active_stream=True)
    session = captured["payload"]["session"]

    assert captured["status"] == 200
    assert captured["lookup_calls"] == 0
    assert captured["message_limit"] == 0
    assert captured["snapshot_calls"] == 0
    assert session["messages"] == []
    assert session["message_count"] == 100
    assert "runtime_journal_snapshot" not in session


def test_sidecarless_tail_honors_msg_limit_and_reports_absolute_cursor(monkeypatch):
    captured = _invoke_sidecarless_detail(monkeypatch, "messages=1&msg_limit=8")
    session = captured["payload"]["session"]

    assert captured["message_limit"] == 8
    assert captured["message_before"] is None
    assert [row["content"] for row in session["messages"]] == [
        f"message-{idx}" for idx in range(92, 100)
    ]
    assert session["message_count"] == 100
    assert session["_messages_offset"] == 92
    assert session["_messages_truncated"] is True


def test_sidecarless_before_page_honors_absolute_cursor(monkeypatch):
    captured = _invoke_sidecarless_detail(
        monkeypatch,
        "messages=1&msg_limit=8&msg_before=50",
    )
    session = captured["payload"]["session"]

    assert captured["message_limit"] == 8
    assert captured["message_before"] == 50
    assert [row["content"] for row in session["messages"]] == [
        f"message-{idx}" for idx in range(42, 50)
    ]
    assert session["message_count"] == 100
    assert session["_messages_offset"] == 42
    assert session["_messages_truncated"] is True


def test_persisted_metadata_only_also_omits_runtime_snapshot(monkeypatch):
    import api.models as models
    import api.routes as routes

    sid = "persisted_metadata_projection"
    stream_id = "stream-persisted-metadata"
    session = models.Session(
        session_id=sid,
        title="Persisted metadata",
        workspace="/tmp",
        model="test-model",
        messages=[{"role": "user", "content": "hidden", "timestamp": 1.0}],
        active_stream_id=stream_id,
        created_at=1.0,
        updated_at=1.0,
    )
    captured = {"snapshot_calls": 0}

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "_metadata_only_message_summary", lambda *_args, **_kwargs: {"message_count": 1, "last_message_at": 1.0})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {stream_id})
    monkeypatch.setattr(routes, "get_latest_state_db_compaction_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "find_run_summary", lambda *_args, **_kwargs: {"status": "running"})
    monkeypatch.setattr(routes, "_run_journal_status_payload", lambda *_args, **_kwargs: {"status": "running"})

    def snapshot(*_args, **_kwargs):
        captured["snapshot_calls"] += 1
        return {"messages": [{"role": "assistant", "content": "large"}]}

    monkeypatch.setattr(routes, "_run_journal_live_snapshot", snapshot)

    def fake_j(_handler, payload, status=200, **_kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urlparse(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    assert routes.handle_get(SimpleNamespace(), parsed) is True

    assert captured["status"] == 200
    assert captured["snapshot_calls"] == 0
    assert captured["payload"]["session"]["messages"] == []
    assert "runtime_journal_snapshot" not in captured["payload"]["session"]
