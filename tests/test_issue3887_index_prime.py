"""Regression tests for #3887 — missing-index sidebar behaviour.

The sidebar's CLI-session scan (``read_importable_agent_session_rows``) orders
candidate sessions by a correlated ``MAX(mx.timestamp)`` subquery over the
``messages`` table. That is fast only when the agent's standard
``idx_messages_session ON messages(session_id, timestamp)`` index exists. A
state.db that lost its migrations (older hermes-agent, or a hand-rebuilt /
reimported db) has no such index and the scan degrades to a full ``messages``
scan per candidate session — stalling ``/api/sessions`` for seconds on every
refresh.

The listing path must never build indexes itself: CREATE INDEX can retain the
SQLite writer lock for minutes on a multi-GiB database. These tests assert that
missing-index databases remain read-only and degrade to denormalized session
metadata, while existing covering indexes still enable the exact projection.
"""
import os
import sqlite3
import stat

import pytest

import api.agent_sessions as agent_sessions


def _full_schema_db(path):
    """A state.db with the columns the scan reads, but NO messages index."""
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE sessions(
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, session_source TEXT,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT,
            user_id TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT,
            session_key TEXT, origin_chat_id TEXT, origin_user_id TEXT,
            platform TEXT)"""
    )
    cur.execute(
        """CREATE TABLE messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL)"""
    )
    for i in range(3):
        sid = f"sess{i}"
        cur.execute(
            "INSERT INTO sessions(id, title, model, message_count, started_at, "
            "source, session_source) VALUES (?,?,?,?,?,?,?)",
            (sid, f"Session {i}", "m", 2, 1000.0 + i, "cli", "cli"),
        )
        cur.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) "
            "VALUES (?,?,?,?)",
            (sid, "user", "hi", 1001.0 + i),
        )
        cur.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) "
            "VALUES (?,?,?,?)",
            (sid, "assistant", "yo", 1002.0 + i),
        )
    conn.commit()
    conn.close()


def _messages_indexes(path):
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='messages'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_listing_does_not_create_missing_indexes(tmp_path, monkeypatch):
    """A missing index must not turn a sidebar read into a long writer lock."""
    db = tmp_path / "state.db"
    _full_schema_db(db)
    assert "idx_messages_session" not in _messages_indexes(db)
    connect_calls = []
    real_connect = agent_sessions.sqlite3.connect

    def recording_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", recording_connect)

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )
    listing_connect_calls = list(connect_calls)

    # Listing still returns the sessions from denormalized metadata ...
    assert {r["id"] for r in rows} == {"sess0", "sess1", "sess2"}
    # ... without mutating schema or opening a write-capable connection.
    assert "idx_messages_session" not in _messages_indexes(db)
    assert "idx_messages_session_role" not in _messages_indexes(db)
    assert len(listing_connect_calls) == 1
    assert "mode=ro" in str(listing_connect_calls[0][0][0])
    assert listing_connect_calls[0][1].get("uri") is True


def test_existing_indexes_enable_exact_projection_without_mutation(tmp_path):
    """Existing covering indexes are reused untouched."""
    db = tmp_path / "state.db"
    _full_schema_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE INDEX idx_messages_session ON messages(session_id, timestamp)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_session_role "
        "ON messages(session_id, role COLLATE NOCASE)"
    )
    conn.commit()
    conn.close()
    before = _messages_indexes(db)

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )

    assert {r["id"] for r in rows} == {"sess0", "sess1", "sess2"}
    # Index set is unchanged — no duplicate, no error.
    assert _messages_indexes(db) == before
    assert "idx_messages_session" in _messages_indexes(db)
    assert "idx_messages_session_role" in _messages_indexes(db)


@pytest.mark.parametrize(
    "index_sql",
    (
        "CREATE INDEX idx_messages_session "
        "ON messages(session_id, timestamp) WHERE role = 'user'",
        "CREATE INDEX idx_messages_session ON messages(timestamp, session_id)",
        "CREATE INDEX idx_messages_session_role ON messages(session_id, role)",
        "CREATE INDEX idx_messages_session_role "
        "ON messages(session_id, role COLLATE NOCASE) WHERE role = 'user'",
    ),
)
def test_malformed_same_name_index_degrades_without_forced_plan(
    tmp_path, index_sql
):
    db = tmp_path / "state.db"
    _full_schema_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(index_sql)
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )

    assert {row["id"] for row in rows} == {"sess0", "sess1", "sess2"}
    assert _messages_indexes(db) == {
        "idx_messages_session"
        if "idx_messages_session ON" in index_sql
        else "idx_messages_session_role"
    }


def test_missing_timestamp_column_does_not_trigger_index_creation(tmp_path):
    """A minimal messages schema (no timestamp column) must not attempt the
    index and must not crash the listing."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE sessions(
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT)"""
    )
    cur.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT)"
    )
    cur.execute(
        "INSERT INTO sessions(id, title, model, message_count, started_at, "
        "source) VALUES ('s1','T','m',1,1000.0,'cli')"
    )
    cur.execute(
        "INSERT INTO messages(id, session_id, role, content) "
        "VALUES (1,'s1','user','hi')"
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )

    # Listing degrades gracefully via the denormalized counts (still surfaces).
    assert {r["id"] for r in rows} == {"s1"}
    # The timestamp-less schema must NOT have an index primed on it.
    assert "idx_messages_session" not in _messages_indexes(db)


def test_listing_degrades_on_readonly_db(tmp_path):
    """A read-only db without indexes still returns denormalized rows."""
    # Root bypasses chmod; non-root exercises the same always-read-only listing.
    if hasattr(os, "getuid") and os.getuid() == 0:
        pytest.skip("chmod-based read-only test is a no-op under root")
    db = tmp_path / "state.db"
    _full_schema_db(db)
    os.chmod(db, stat.S_IREAD)
    try:
        rows = agent_sessions.read_importable_agent_session_rows(
            db, limit=20, exclude_sources=None
        )
        assert {r["id"] for r in rows} == {"sess0", "sess1", "sess2"}
    finally:
        # Restore write so tmp_path cleanup can remove it.
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)
