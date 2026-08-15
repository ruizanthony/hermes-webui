"""Tests for explicit, drained state.db WebUI read-index maintenance."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ensure_state_db_read_indexes.py"
FINGERPRINT = "idx_sessions_webui_fingerprint"
TIMESTAMP = "idx_messages_session"
ROLE = "idx_messages_session_role"


def _make_db(path: Path, *, include_activity: bool = True) -> None:
    activity_column = ", last_activity_at REAL" if include_activity else ""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, source TEXT NOT NULL, message_count INTEGER DEFAULT 0"
        f"{activity_column})"
    )
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, message_count"
        + (", last_activity_at" if include_activity else "")
        + ") VALUES ('s1', 'telegram', 2"
        + (", 123.0" if include_activity else "")
        + ")"
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, timestamp) VALUES (?, ?, ?)",
        [("s1", "user", 123.0), ("s1", "assistant", 124.0)],
    )
    conn.commit()
    conn.close()


def _run(
    db: Path,
    *,
    confirm: bool = True,
    activity_lock: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    lock = activity_lock or db.parent / "turns.lock"
    command = [
        sys.executable,
        str(SCRIPT),
        "--db",
        str(db),
        "--timeout-ms",
        "5000",
        "--activity-lock",
        str(lock),
    ]
    if confirm:
        command.append("--confirm-drained")
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _key_definition(conn: sqlite3.Connection, name: str):
    return tuple(
        (str(row[2]), str(row[4] or "BINARY").upper(), int(row[3]))
        for row in conn.execute(f"PRAGMA index_xinfo({name})")
        if int(row[5]) == 1
    )


def test_script_creates_verifies_and_reuses_all_covering_indexes(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)

    first = _run(db)
    assert first.returncode == 0, first.stderr or first.stdout
    first_payload = json.loads(first.stdout)
    assert first_payload["indexes"] == {
        FINGERPRINT: "created",
        ROLE: "created",
        TIMESTAMP: "created",
    }

    second = _run(db)
    assert second.returncode == 0, second.stderr or second.stdout
    assert set(json.loads(second.stdout)["indexes"].values()) == {"existing"}

    conn = sqlite3.connect(db)
    assert _key_definition(conn, FINGERPRINT) == (
        ("source", "BINARY", 0),
        ("id", "BINARY", 0),
        ("message_count", "BINARY", 0),
        ("last_activity_at", "BINARY", 0),
    )
    assert _key_definition(conn, TIMESTAMP) == (
        ("session_id", "BINARY", 0),
        ("timestamp", "BINARY", 0),
    )
    assert _key_definition(conn, ROLE) == (
        ("session_id", "BINARY", 0),
        ("role", "NOCASE", 0),
    )
    queries = (
        (
            "SELECT source, id, message_count, last_activity_at FROM sessions "
            f"INDEXED BY {FINGERPRINT} WHERE source IS NOT NULL "
            "AND source NOT IN (?, ?) ORDER BY source, id",
            ("cron", "webui"),
            FINGERPRINT,
        ),
        (
            "SELECT COUNT(*), MAX(timestamp) FROM messages "
            f"INDEXED BY {TIMESTAMP} WHERE session_id = ?",
            ("s1",),
            TIMESTAMP,
        ),
        (
            f"SELECT 1 FROM messages INDEXED BY {ROLE} "
            "WHERE session_id = ? AND role COLLATE NOCASE = 'user' LIMIT 2",
            ("s1",),
            ROLE,
        ),
    )
    for sql, params, name in queries:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
        joined = " | ".join(plan).upper()
        assert f"COVERING INDEX {name.upper()}" in joined, plan
        assert "TEMP B-TREE" not in joined, plan
    conn.close()


def test_script_requires_explicit_drain_confirmation(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)

    result = _run(db, confirm=False)

    assert result.returncode == 1
    assert "--confirm-drained" in json.loads(result.stdout)["error"]


def test_script_rejects_busy_agent_turn_lock(tmp_path):
    db = tmp_path / "state.db"
    lock_path = tmp_path / "turns.lock"
    _make_db(db)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        result = _run(db, activity_lock=lock_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.returncode == 1
    assert "active Hermes turn" in json.loads(result.stdout)["error"]


def test_script_fails_closed_when_required_column_is_missing(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db, include_activity=False)

    result = _run(db)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "last_activity_at" in payload["error"]
    conn = sqlite3.connect(db)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        )
    }
    assert not ({FINGERPRINT, TIMESTAMP, ROLE} & names)
    conn.close()


def test_script_rejects_preexisting_index_with_wrong_shape(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE INDEX {FINGERPRINT} ON sessions(source, id)")
    conn.commit()
    conn.close()

    result = _run(db)

    assert result.returncode == 1
    assert "unexpected key definition" in json.loads(result.stdout)["error"]


def test_script_rejects_name_collision_on_wrong_table(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE impostor (source TEXT, id TEXT, message_count INTEGER, last_activity_at REAL)")
    conn.execute(
        f"CREATE INDEX {FINGERPRINT} ON impostor(source, id, message_count, last_activity_at)"
    )
    conn.commit()
    conn.close()

    result = _run(db)

    assert result.returncode == 1
    assert "unexpected table" in json.loads(result.stdout)["error"]


def test_script_rejects_wrong_role_collation(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE INDEX {ROLE} ON messages(session_id, role COLLATE BINARY)")
    conn.commit()
    conn.close()

    result = _run(db)

    assert result.returncode == 1
    assert "unexpected key definition" in json.loads(result.stdout)["error"]


def test_script_rejects_missing_database_without_creating_it(tmp_path):
    db = tmp_path / "missing-state.db"

    result = _run(db)

    assert result.returncode == 1
    assert not db.exists()
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "does not exist" in payload["error"]
