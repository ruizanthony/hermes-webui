from __future__ import annotations

import io
import json
import time
from pathlib import Path

import api.worktree_gc_git as git_backend
from api.worktree_gc_inventory import (
    HealthProbe,
    ProcessScan,
    audit_managed_worktrees,
    revalidate_managed_worktree_candidate,
)
from scripts import worktree_gc as cli
from tests.test_worktree_gc_git_classification import (
    _git,
    add_worktree,
    make_remote_repo,
)


def _audit_without_host_activity(**kwargs):
    return audit_managed_worktrees(
        **kwargs,
        health_probe=lambda _url: HealthProbe(True, 0),
        process_scan=ProcessScan(True, True, ()),
    )


def _revalidate_without_host_activity(**kwargs):
    return revalidate_managed_worktree_candidate(
        **kwargs,
        health_probe=lambda _url: HealthProbe(True, 0),
        process_scan_fn=lambda: ProcessScan(True, True, ()),
    )


def _write_archived_sidecar(
    state_dir: Path,
    *,
    session_id: str,
    worktree: Path,
    branch: str,
    repo: Path,
) -> None:
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True)
    old_timestamp = time.time() - (8 * 24 * 60 * 60)
    (sessions / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "profile": "default",
                "archived": True,
                "worktree_path": str(worktree),
                "worktree_branch": branch,
                "worktree_repo_root": str(repo),
                "worktree_created_at": old_timestamp,
                "updated_at": old_timestamp,
                "active_stream_id": None,
                "pending_user_message": None,
                "pending_attachments": None,
                "pending_started_at": None,
            }
        ),
        encoding="utf-8",
    )


def test_real_git_backend_dry_run_then_collects_archived_ancestor(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/integration-ancestor"
    worktree = add_worktree(case, tmp_path, branch)
    state_dir = tmp_path / "state"
    report_path = tmp_path / "reports" / "worktree-gc.json"
    _write_archived_sidecar(
        state_dir,
        session_id="integration-session",
        worktree=worktree,
        branch=branch,
        repo=repo,
    )

    common_args = [
        "--repo",
        str(repo),
        "--state-dir",
        str(state_dir),
        "--health-url",
        "http://unused.invalid/health",
        "--report-path",
        str(report_path),
        "--min-age-days",
        "7",
    ]

    dry_run_stdout = io.StringIO()
    dry_run_rc = cli.main(
        [*common_args, "--dry-run", "--json"],
        git_backend=git_backend,
        audit_fn=_audit_without_host_activity,
        revalidate_fn=_revalidate_without_host_activity,
        stdout=dry_run_stdout,
    )
    dry_run_report = json.loads(dry_run_stdout.getvalue())

    assert dry_run_rc == 0
    assert dry_run_report["mode"] == "dry-run"
    assert dry_run_report["collection_allowed"] is True
    assert dry_run_report["counts"]["eligible"] == 1
    assert dry_run_report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"
    assert worktree.is_dir()
    assert _git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        check=False,
    ).returncode == 0

    collect_stdout = io.StringIO()
    collect_rc = cli.main(
        [*common_args, "--collect", "--json"],
        git_backend=git_backend,
        audit_fn=_audit_without_host_activity,
        revalidate_fn=_revalidate_without_host_activity,
        stdout=collect_stdout,
    )
    collect_report = json.loads(collect_stdout.getvalue())
    persisted_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert collect_rc == 0
    assert collect_report == persisted_report
    assert collect_report["collection"] == {
        "attempted": 1,
        "collected": 1,
        "failed": 0,
        "skipped": 0,
    }
    assert collect_report["candidates"][0]["collection"]["status"] == "collected"
    result = collect_report["candidates"][0]["collection"]["result"]
    assert result["removed_worktree"] is True
    assert result["branch_deleted"] is True
    assert result["branch_kept"] is False
    assert not worktree.exists()
    assert _git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        check=False,
    ).returncode != 0
