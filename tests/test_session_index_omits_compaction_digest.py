"""Regression tests: the sidebar index must not persist compaction digests.

``compression_anchor_summary`` is a multi-KB per-session compaction digest.
On this deployment it accounted for 89% of a 10.4 MB ``_index.json``
(7.02 MB across 1558 rows) even though no index consumer reads it: the WebUI
renders the compaction card from ``/api/session`` (sidecar-backed), and every
backend index reader uses identity/lineage/count fields only.

Because the index is re-parsed, re-serialised and fsync'd on *every* session
save, that dead weight was a fixed CPU tax paid by small and large
conversations alike — and, since HTTP handlers and agent workers share one
Python process, a GIL-held serial cost that throttles parallel conversations.

These tests pin the projection so the field cannot silently creep back in.
"""

import json

import pytest

from api import models


@pytest.fixture
def index_env(tmp_path, monkeypatch):
    """Isolate SESSION_DIR / SESSION_INDEX_FILE and the in-memory registry."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    return session_dir, index_file


def _write_sidecar(session_dir, sid, **extra):
    payload = {
        "session_id": sid,
        "title": f"Session {sid}",
        "messages": [{"role": "user", "content": "hello"}],
        "message_count": 1,
        "updated_at": 10,
    }
    payload.update(extra)
    (session_dir / f"{sid}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return payload


def _read_index(index_file):
    return json.loads(index_file.read_text(encoding="utf-8"))


def test_full_rebuild_omits_compaction_digest(index_env):
    session_dir, index_file = index_env
    _write_sidecar(
        session_dir,
        "sess-a",
        compression_anchor_summary="D" * 5000,
        compression_anchor_message_key="key-a",
    )

    models._write_session_index(updates=None)

    rows = _read_index(index_file)
    assert len(rows) == 1
    row = rows[0]
    assert "compression_anchor_summary" not in row
    # Identity, lineage and count fields — the ones readers actually use —
    # must survive the projection.
    assert row["session_id"] == "sess-a"
    assert row["message_count"] == 1
    assert row["compression_anchor_message_key"] == "key-a"


def test_fast_path_update_omits_compaction_digest(index_env):
    session_dir, index_file = index_env
    _write_sidecar(session_dir, "sess-a")
    models._write_session_index(updates=None)

    session = models.Session(session_id="sess-a", title="Session sess-a")
    session.compression_anchor_summary = "E" * 4096
    models._write_session_index(updates=[session])

    rows = _read_index(index_file)
    by_id = {r["session_id"]: r for r in rows}
    assert "compression_anchor_summary" not in by_id["sess-a"]


def test_projection_does_not_mutate_caller_entry():
    """``Session.save()`` passes compact entries it still owns."""
    entry = {"session_id": "sess-a", "compression_anchor_summary": "keep me"}
    projected = models._index_entries_payload([entry])

    assert "compression_anchor_summary" not in projected[0]
    # The caller's dict must be untouched.
    assert entry["compression_anchor_summary"] == "keep me"


def test_projection_preserves_non_dict_and_clean_entries():
    clean = {"session_id": "sess-b"}
    entries = [clean, "not-a-dict", None]
    projected = models._index_entries_payload(entries)

    # Clean dicts are passed through by identity (no needless copy).
    assert projected[0] is clean
    assert projected[1] == "not-a-dict"
    assert projected[2] is None


def test_prune_strips_digest_from_surviving_rows(index_env):
    """Legacy rows written before this change are cleaned on next rewrite."""
    session_dir, index_file = index_env
    _write_sidecar(session_dir, "sess-a")
    _write_sidecar(session_dir, "sess-b")
    # Simulate a pre-existing index that still carries the digest.
    index_file.write_text(
        json.dumps(
            [
                {"session_id": "sess-a", "compression_anchor_summary": "old"},
                {"session_id": "sess-b", "compression_anchor_summary": "old"},
            ]
        ),
        encoding="utf-8",
    )

    models.prune_session_from_index("sess-a")

    rows = _read_index(index_file)
    assert [r["session_id"] for r in rows] == ["sess-b"]
    assert "compression_anchor_summary" not in rows[0]


def test_sidecar_still_carries_digest(index_env):
    """The projection is index-only: the sidecar remains the source of truth."""
    session_dir, index_file = index_env
    _write_sidecar(
        session_dir, "sess-a", compression_anchor_summary="S" * 128
    )
    models._write_session_index(updates=None)

    assert "compression_anchor_summary" not in _read_index(index_file)[0]

    sidecar = json.loads(
        (session_dir / "sess-a.json").read_text(encoding="utf-8")
    )
    assert sidecar["compression_anchor_summary"] == "S" * 128


def test_compact_still_exposes_digest_for_api_session():
    """``/api/session`` builds the compaction card from ``compact()``."""
    session = models.Session(session_id="sess-a", title="t")
    session.compression_anchor_summary = "visible in the card"

    assert session.compact()["compression_anchor_summary"] == "visible in the card"
