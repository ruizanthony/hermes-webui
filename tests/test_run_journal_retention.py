"""Run-journal retention: expired journals are pruned, active sessions are safe.

Regression context (2026-08-17): SESSION_DIR/_run_journal was never pruned and
grew to 7.4 GB. Retention must delete only files older than the window, never
touch sessions with an active run, and fail closed when the active-run table
is unreadable.
"""

import time
from pathlib import Path

import pytest

from api import run_journal_retention as R


@pytest.fixture()
def journal_tree(tmp_path, monkeypatch):
    root = tmp_path / "_run_journal"
    root.mkdir()
    # Isolate ACTIVE_RUNS for each test.
    from api import config as C
    monkeypatch.setattr(C, "ACTIVE_RUNS", {}, raising=True)
    return tmp_path


def _mk_journal(root: Path, sid: str, run: str, *, age_days: float, size: int = 100) -> Path:
    d = root / "_run_journal" / sid
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{run}.jsonl"
    f.write_bytes(b"x" * size)
    old = time.time() - age_days * 86400
    import os
    os.utime(f, (old, old))
    return f


def test_expired_journals_are_deleted_and_empty_dirs_removed(journal_tree):
    f_old = _mk_journal(journal_tree, "sess_old", "run1", age_days=30)
    f_new = _mk_journal(journal_tree, "sess_new", "run2", age_days=1)

    stats = R.prune_run_journals(session_dir=journal_tree, max_age_days=14)

    assert not f_old.exists()
    assert not f_old.parent.exists()  # empty dir removed
    assert f_new.exists()
    assert stats["deleted"] == 1
    assert stats["kept"] == 1
    assert stats["dirs_removed"] == 1
    assert stats["bytes_freed"] == 100


def test_active_session_is_never_pruned(journal_tree):
    from api import config as C
    f_old = _mk_journal(journal_tree, "sess_active", "run1", age_days=365)
    with C.ACTIVE_RUNS_LOCK:
        C.ACTIVE_RUNS["stream-1"] = {"session_id": "sess_active", "phase": "running"}
    try:
        stats = R.prune_run_journals(session_dir=journal_tree, max_age_days=14)
    finally:
        with C.ACTIVE_RUNS_LOCK:
            C.ACTIVE_RUNS.clear()

    assert f_old.exists()
    assert stats["deleted"] == 0
    assert stats["skipped_active"] == 1


def test_zero_retention_disables_pruning(journal_tree):
    f_old = _mk_journal(journal_tree, "sess_old", "run1", age_days=400)
    stats = R.prune_run_journals(session_dir=journal_tree, max_age_days=0)
    assert f_old.exists()
    assert stats["deleted"] == 0 and stats["scanned"] == 0


def test_unreadable_active_runs_fails_closed(journal_tree, monkeypatch):
    f_old = _mk_journal(journal_tree, "sess_old", "run1", age_days=400)
    monkeypatch.setattr(R, "_active_session_ids", lambda: None)
    stats = R.prune_run_journals(session_dir=journal_tree, max_age_days=14)
    assert f_old.exists()
    assert stats["deleted"] == 0


def test_non_jsonl_files_keep_directory_alive(journal_tree):
    f_old = _mk_journal(journal_tree, "sess_old", "run1", age_days=30)
    stray = f_old.parent / "notes.txt"
    stray.write_text("keep")
    stats = R.prune_run_journals(session_dir=journal_tree, max_age_days=14)
    assert not f_old.exists()
    assert stray.exists()
    assert stray.parent.exists()
    assert stats["dirs_removed"] == 0


def test_worker_start_stop_cycle():
    assert R.start_run_journal_retention_worker() is True
    # Second start is a no-op while alive.
    assert R.start_run_journal_retention_worker() is False
    assert R.stop_run_journal_retention_worker(timeout=5) is True
    # Stopped again: nothing to stop.
    assert R.stop_run_journal_retention_worker(timeout=1) is False
