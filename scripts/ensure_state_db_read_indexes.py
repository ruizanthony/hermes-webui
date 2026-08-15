#!/usr/bin/env python3
"""Install WebUI read indexes during an explicitly drained maintenance window."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

DEFAULT_ACTIVITY_LOCK = Path("/run/lock/hermes-agent-turns.lock")

INDEX_SPECS = {
    "idx_sessions_webui_fingerprint": {
        "table": "sessions",
        "ddl": (
            "CREATE INDEX idx_sessions_webui_fingerprint ON sessions("
            "source COLLATE BINARY, id COLLATE BINARY, "
            "message_count COLLATE BINARY, last_activity_at COLLATE BINARY)"
        ),
        "keys": (
            ("source", "BINARY", 0),
            ("id", "BINARY", 0),
            ("message_count", "BINARY", 0),
            ("last_activity_at", "BINARY", 0),
        ),
        "plan": (
            "SELECT source, id, message_count, last_activity_at "
            "FROM sessions INDEXED BY idx_sessions_webui_fingerprint "
            "WHERE source IS NOT NULL AND source NOT IN (?, ?) "
            "ORDER BY source, id",
            ("cron", "webui"),
        ),
    },
    "idx_messages_session": {
        "table": "messages",
        "ddl": (
            "CREATE INDEX idx_messages_session ON messages("
            "session_id COLLATE BINARY, timestamp COLLATE BINARY)"
        ),
        "keys": (("session_id", "BINARY", 0), ("timestamp", "BINARY", 0)),
        "plan": (
            "SELECT COUNT(*), MAX(timestamp) FROM messages "
            "INDEXED BY idx_messages_session WHERE session_id = ?",
            ("probe",),
        ),
    },
    "idx_messages_session_role": {
        "table": "messages",
        "ddl": (
            "CREATE INDEX idx_messages_session_role ON messages("
            "session_id COLLATE BINARY, role COLLATE NOCASE)"
        ),
        "keys": (("session_id", "BINARY", 0), ("role", "NOCASE", 0)),
        "plan": (
            "SELECT 1 FROM messages INDEXED BY idx_messages_session_role "
            "WHERE session_id = ? AND role COLLATE NOCASE = 'user' LIMIT 2",
            ("probe",),
        ),
    },
}


@contextmanager
def _exclusive_activity_lock(path: Path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"active Hermes turn holds maintenance lock: {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _existing_index(conn: sqlite3.Connection, index_name: str):
    return conn.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()


def _validate_index(conn: sqlite3.Connection, index_name: str, spec: dict) -> None:
    existing = _existing_index(conn, index_name)
    if existing is None:
        raise RuntimeError(f"required index is absent: {index_name}")
    table_name = str(existing[0] or "")
    if table_name != spec["table"]:
        raise RuntimeError(
            f"{index_name} belongs to unexpected table {table_name!r}; "
            f"expected {spec['table']!r}"
        )

    listed = None
    for row in conn.execute(f"PRAGMA index_list({spec['table']})"):
        if str(row[1]) == index_name:
            listed = row
            break
    if listed is None:
        raise RuntimeError(f"{index_name} is not listed on {spec['table']}")
    if int(listed[2]) != 0 or int(listed[4]) != 0:
        raise RuntimeError(f"{index_name} must be non-unique and non-partial")

    keys = tuple(
        (str(row[2]), str(row[4] or "BINARY").upper(), int(row[3]))
        for row in conn.execute(f"PRAGMA index_xinfo({index_name})")
        if int(row[5]) == 1
    )
    if keys != spec["keys"]:
        raise RuntimeError(
            f"{index_name} has unexpected key definition {keys!r}; "
            f"expected {spec['keys']!r}"
        )

    sql, params = spec["plan"]
    details = tuple(
        str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    joined = " | ".join(details).upper()
    if f"COVERING INDEX {index_name.upper()}" not in joined:
        raise RuntimeError(f"{index_name} is not covering for its WebUI query: {details!r}")
    if "TEMP B-TREE" in joined:
        raise RuntimeError(f"{index_name} requires a temporary sort: {details!r}")


def ensure_read_indexes(
    db_path: Path,
    *,
    timeout_ms: int = 120_000,
    confirmed_drained: bool = False,
    activity_lock: Path = DEFAULT_ACTIVITY_LOCK,
) -> dict[str, str]:
    """Create and verify all read indexes while holding the exclusive turn lock."""
    if not confirmed_drained:
        raise RuntimeError("refusing online index maintenance without --confirm-drained")
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"state DB does not exist: {db_path}")

    with _exclusive_activity_lock(activity_lock):
        with closing(
            sqlite3.connect(str(db_path), timeout=max(1, timeout_ms) / 1000)
        ) as conn:
            conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_ms))}")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing_tables = {spec["table"] for spec in INDEX_SPECS.values()} - tables
            if missing_tables:
                raise RuntimeError(
                    "state DB is missing required table(s): "
                    + ", ".join(sorted(missing_tables))
                )

            statuses: dict[str, str] = {}
            for index_name, spec in INDEX_SPECS.items():
                if _existing_index(conn, index_name) is not None:
                    _validate_index(conn, index_name, spec)
                    statuses[index_name] = "existing"

            for index_name, spec in INDEX_SPECS.items():
                if index_name in statuses:
                    continue
                conn.execute(spec["ddl"])
                statuses[index_name] = "created"
            conn.commit()

            for index_name, spec in INDEX_SPECS.items():
                _validate_index(conn, index_name, spec)
            return statuses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".hermes" / "state.db",
        help="Path to Hermes state.db (default: ~/.hermes/state.db)",
    )
    parser.add_argument("--timeout-ms", type=int, default=120_000)
    parser.add_argument("--confirm-drained", action="store_true")
    parser.add_argument(
        "--activity-lock",
        type=Path,
        default=DEFAULT_ACTIVITY_LOCK,
        help="Cooperative Hermes turn lock to acquire exclusively",
    )
    args = parser.parse_args(argv)

    try:
        statuses = ensure_read_indexes(
            args.db,
            timeout_ms=args.timeout_ms,
            confirmed_drained=args.confirm_drained,
            activity_lock=args.activity_lock,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "indexes": statuses,
                "db": str(args.db.expanduser().resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
