import errno
import json
import os
from pathlib import Path

import pytest

from api import worktree_gc_inventory as inventory
from api.worktree_gc_inventory import (
    HealthProbe,
    ProcessCwd,
    ProcessScan,
    scan_process_cwds,
)


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """These unit tests do not need the repository's HTTP server fixture."""


def _proc_cwd(proc_root: Path, pid: int, cwd: Path) -> None:
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True)
    os.symlink(cwd, pid_dir / "cwd")


def test_process_scan_blocks_exact_and_descendant_cwds_but_not_prefix_lookalike(tmp_path):
    proc_root = tmp_path / "proc"
    worktree = tmp_path / "worktrees" / "feature"
    child = worktree / "nested"
    lookalike = tmp_path / "worktrees" / "feature-copy"
    child.mkdir(parents=True)
    lookalike.mkdir(parents=True)
    _proc_cwd(proc_root, 101, worktree)
    _proc_cwd(proc_root, 102, child)
    _proc_cwd(proc_root, 103, lookalike)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is True
    assert scan.process_count == 3
    assert scan.blocking_process_count(worktree) == 2
    assert scan.blocking_process_count(child) == 1
    assert scan.blocking_process_count(lookalike) == 1


def test_process_that_disappears_during_scan_is_not_an_error(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    _proc_cwd(proc_root, 201, cwd)
    (proc_root / "202").mkdir()
    real_readlink = os.readlink

    def disappearing_readlink(path):
        if Path(path).parent.name == "202":
            raise FileNotFoundError(errno.ENOENT, "gone", str(path))
        return real_readlink(path)

    monkeypatch.setattr(os, "readlink", disappearing_readlink)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is True
    assert scan.process_count == 1
    assert scan.unreadable_count == 0


def test_globally_inaccessible_proc_scan_is_uncertain(tmp_path):
    proc_root = tmp_path / "not-a-directory"
    proc_root.write_text("not proc", encoding="utf-8")

    scan = scan_process_cwds(proc_root)

    assert scan.available is False
    assert scan.complete is False
    assert scan.process_count == 0
    assert scan.blocking_process_count(tmp_path) == 0


def test_unreadable_pid_cwd_makes_scan_incomplete(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    (proc_root / "301").mkdir(parents=True)

    def denied_readlink(path):
        raise PermissionError(errno.EACCES, "denied", str(path))

    monkeypatch.setattr(os, "readlink", denied_readlink)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is False
    assert scan.unreadable_count == 1


def _write_archived_session(
    state_dir: Path,
    repo: Path,
    worktree: Path,
) -> dict:
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True)
    payload = {
        "session_id": "process-race",
        "profile": "default",
        "archived": True,
        "updated_at": 1_700_000_000,
        "worktree_path": str(worktree),
        "worktree_branch": "hermes/process-race",
        "worktree_repo_root": str(repo),
        "worktree_created_at": 1_700_000_000,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
    }
    (sessions / "process-race.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return {
        "session_id": "process-race",
        "session_ids": ["process-race"],
        "profile": "default",
        "worktree_path": str(worktree.resolve()),
        "worktree_branch": "hermes/process-race",
        "worktree_repo_root": str(repo.resolve()),
        "worktree_created_at": 1_700_000_000.0,
        "age_days": 900.0,
        "age_source": "worktree_created_at",
        "verdict": "REMOVE_ANCESTOR",
        "eligible": True,
        "reasons": ["branch_is_ancestor"],
    }


def test_revalidation_refuses_process_cwd_appearing_after_audit(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    audited = _write_archived_session(state_dir, repo, worktree)

    result = inventory.revalidate_managed_worktree_candidate(
        audited_candidate=audited,
        state_dir=state_dir,
        profile="default",
        repo_filter=repo,
        min_age_days=7,
        health_url="http://127.0.0.1:8787/health",
        health_probe=lambda _url: HealthProbe(True, 0),
        process_scan_fn=lambda: ProcessScan(
            available=True,
            complete=True,
            process_cwds=(ProcessCwd(999, str(worktree)),),
        ),
        now=1_780_000_000,
    )

    assert result.allowed is False
    assert result.global_guard is False
    assert result.reason == "candidate_runtime_guard"
    assert result.candidate_reasons == ("process_cwd_in_worktree",)


def test_revalidation_makes_incomplete_process_scan_a_global_guard(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    audited = _write_archived_session(state_dir, repo, worktree)

    result = inventory.revalidate_managed_worktree_candidate(
        audited_candidate=audited,
        state_dir=state_dir,
        profile="default",
        repo_filter=repo,
        min_age_days=7,
        health_url="http://127.0.0.1:8787/health",
        health_probe=lambda _url: HealthProbe(True, 0),
        process_scan_fn=lambda: ProcessScan(
            available=True,
            complete=False,
            process_cwds=(),
            unreadable_count=1,
        ),
        now=1_780_000_000,
    )

    assert result.allowed is False
    assert result.global_guard is True
    assert result.reason == "global_runtime_guard"
    assert result.global_reasons == ("process_scan_incomplete",)
