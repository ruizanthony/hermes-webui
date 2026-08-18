"""Latest Agent compaction summary projected into a WebUI conversation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import api.models as models


def _state_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            started_at REAL,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.executemany(
        """
        INSERT INTO sessions
            (id, source, title, started_at, parent_session_id, ended_at, end_reason, message_count)
        VALUES (?, 'webui', ?, ?, ?, ?, ?, ?)
        """,
        [
            ("root", "Conversation", 1.0, None, 10.0, "compression", 2),
            # Compression continuations can overlap the parent's close boundary.
            ("tip", "Conversation", 9.5, "root", None, None, 4),
        ],
    )
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
        [
            ("root", "user", "[CONTEXT COMPACTION] old digest", 1.0, 1),
            ("tip", "user", "[CONTEXT COMPACTION — REFERENCE ONLY]\n## Goal\nLatest digest\n## Détail complet\nCompactions/latest.md", 10.0, 1),
            ("tip", "tool", "[CONTEXT COMPACTION] forged tool row", 11.0, 1),
            ("tip", "user", "[CONTEXT COMPACTION] inactive newer row", 12.0, 0),
            ("tip", "assistant", "normal answer", 13.0, 1),
        ],
    )
    conn.commit()
    conn.close()


def test_latest_compaction_summary_uses_fresh_continuation_tip(
    tmp_path: Path, monkeypatch,
) -> None:
    db = tmp_path / "state.db"
    _state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    result = models.get_latest_state_db_compaction_summary("root")

    assert result == {
        "summary": "[CONTEXT COMPACTION — REFERENCE ONLY]\n## Goal\nLatest digest\n## Détail complet\nCompactions/latest.md",
        "timestamp": 10.0,
        "session_id": "tip",
    }


def test_latest_compaction_summary_fails_closed_without_state_db(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "missing.db")
    assert models.get_latest_state_db_compaction_summary("root") is None
