from __future__ import annotations

import subprocess
from pathlib import Path

from api.worktree_gc_git import (
    KEEP_DIRTY,
    KEEP_UNIQUE_COMMITS,
    PRUNE_STALE_METADATA,
    REMOVE_ANCESTOR,
    REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH,
    classify_git_worktree,
    collect_git_worktree,
)
from tests.test_worktree_gc_git_classification import (
    _commit,
    _git,
    add_worktree,
    make_remote_repo,
)


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )


def _listed(repo: Path, worktree: Path) -> bool:
    return str(worktree.resolve()) in _git(
        repo,
        "worktree",
        "list",
        "--porcelain",
    ).stdout


def test_dry_run_reclassifies_but_mutates_nothing(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/dry-run"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)
    assert decision.verdict == REMOVE_ANCESTOR

    result = collect_git_worktree(decision)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["refused"] is False
    assert result["idempotent"] is False
    assert result["action"] == "dry_run"
    assert result["initial_verdict"] == REMOVE_ANCESTOR
    assert result["current_verdict"] == REMOVE_ANCESTOR
    assert result["removed_worktree"] is False
    assert result["branch_deleted"] is False
    assert result["branch_kept"] is True
    assert result["errors"] == []
    assert worktree.is_dir()
    assert _listed(repo, worktree)
    assert _branch_exists(repo, branch)


def test_locked_ancestor_is_unlocked_removed_and_deleted_with_branch_d(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/locked-ancestor"
    worktree = add_worktree(case, tmp_path, branch)
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        "test lock",
        str(worktree),
    )
    decision = classify_git_worktree(worktree, branch, repo)
    real_run_git = gc_git._run_git
    mutation_calls: list[list[str]] = []

    def record_mutations(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args[:2] == ["worktree", "unlock"]:
            mutation_calls.append(args)
        elif args[:2] == ["worktree", "remove"]:
            mutation_calls.append(args)
        elif args[:2] == ["branch", "-d"]:
            mutation_calls.append(args)
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", record_mutations)

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is True
    assert result["action"] == "removed"
    assert result["removed_worktree"] is True
    assert result["branch_deleted"] is True
    assert result["branch_kept"] is False
    assert result["errors"] == []
    assert mutation_calls == [
        ["worktree", "unlock", str(worktree.resolve())],
        ["worktree", "remove", str(worktree.resolve())],
        ["branch", "-d", "--", branch],
    ]
    assert not worktree.exists()
    assert not _listed(repo, worktree)
    assert not _branch_exists(repo, branch)


def test_collection_does_not_execute_repository_hooks(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/no-hooks"
    worktree = add_worktree(case, tmp_path, branch)
    marker = tmp_path / "hook-executed"
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    decision = classify_git_worktree(worktree, branch, repo)
    assert not marker.exists()

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is True
    assert result["branch_deleted"] is True
    assert not marker.exists()


def test_unlock_failure_is_soft_and_remove_never_uses_force(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/unlocked"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)

    result = collect_git_worktree(
        decision,
        dry_run=False,
        delete_ancestor_branch=False,
    )

    assert result["ok"] is True
    assert result["removed_worktree"] is True
    assert result["branch_deleted"] is False
    assert result["branch_kept"] is True
    assert "worktree_unlock_failed" in result["warnings"]
    assert not worktree.exists()
    assert _branch_exists(repo, branch)


def test_patch_equivalent_removes_worktree_but_always_keeps_branch(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/patch-collect"
    worktree = add_worktree(case, tmp_path, branch)
    _commit(
        worktree,
        "shared.txt",
        "same patch\n",
        "equivalent patch under another sha",
    )
    decision = classify_git_worktree(worktree, branch, repo)
    assert decision.verdict == REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH

    result = collect_git_worktree(
        decision,
        dry_run=False,
        delete_ancestor_branch=True,
    )

    assert result["ok"] is True
    assert result["removed_worktree"] is True
    assert result["branch_deleted"] is False
    assert result["branch_kept"] is True
    assert not worktree.exists()
    assert _branch_exists(repo, branch)


def test_unique_commit_is_refused_without_any_mutation(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/unique-refusal"
    worktree = add_worktree(case, tmp_path, branch)
    _commit(worktree, "unique.txt", "must stay\n", "unique")
    decision = classify_git_worktree(worktree, branch, repo)
    assert decision.verdict == KEEP_UNIQUE_COMMITS

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is False
    assert result["refused"] is True
    assert result["action"] == "refused"
    assert result["refusal_reason"] == "decision_not_eligible"
    assert result["removed_worktree"] is False
    assert result["branch_deleted"] is False
    assert result["branch_kept"] is True
    assert worktree.is_dir()
    assert _listed(repo, worktree)
    assert _branch_exists(repo, branch)


def test_stale_metadata_is_audit_only_and_never_pruned_globally(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/stale-refusal"
    worktree = add_worktree(case, tmp_path, branch)
    worktree.rename(tmp_path / "held-stale-worktree")
    decision = classify_git_worktree(worktree, branch, repo)
    assert decision.verdict == PRUNE_STALE_METADATA

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is False
    assert result["refused"] is True
    assert result["refusal_reason"] == "decision_not_eligible"
    assert result["removed_worktree"] is False
    assert result["branch_deleted"] is False
    assert _listed(repo, worktree)
    assert _branch_exists(repo, branch)


def test_toctou_dirty_change_after_audit_is_refused(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/toctou"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)
    assert decision.verdict == REMOVE_ANCESTOR
    (worktree / "arrived-after-audit.txt").write_text("do not lose\n", encoding="utf-8")

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is False
    assert result["refused"] is True
    assert result["refusal_reason"] == "decision_changed"
    assert result["initial_verdict"] == REMOVE_ANCESTOR
    assert result["current_verdict"] == KEEP_DIRTY
    assert result["removed_worktree"] is False
    assert result["branch_deleted"] is False
    assert worktree.is_dir()
    assert (worktree / "arrived-after-audit.txt").read_text(encoding="utf-8") == "do not lose\n"
    assert _branch_exists(repo, branch)


def test_change_arriving_after_reclassification_is_still_not_forced(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/toctou-last-gap"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)
    real_run_git = gc_git._run_git
    injected = False

    def inject_after_reclassification(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        nonlocal injected
        result = real_run_git(args, cwd, timeout=timeout)
        if args[:2] == ["worktree", "unlock"] and not injected:
            injected = True
            (worktree / "late-untracked.txt").write_text(
                "must survive\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(gc_git, "_run_git", inject_after_reclassification)

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is False
    assert result["action"] == "error"
    assert result["errors"] == ["worktree_remove_failed"]
    assert result["removed_worktree"] is False
    assert worktree.is_dir()
    assert (worktree / "late-untracked.txt").read_text(encoding="utf-8") == "must survive\n"
    assert _branch_exists(repo, branch)


def test_remove_failure_is_structured_and_preserves_branch(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/remove-error"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)
    real_run_git = gc_git._run_git

    def fail_remove(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(
                ["git", *args],
                1,
                stdout=b"",
                stderr=b"simulated refusal",
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", fail_remove)

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is False
    assert result["refused"] is False
    assert result["action"] == "error"
    assert result["removed_worktree"] is False
    assert result["branch_deleted"] is False
    assert result["branch_kept"] is True
    assert result["errors"] == ["worktree_remove_failed"]
    assert worktree.is_dir()
    assert _branch_exists(repo, branch)


def test_branch_delete_failure_reports_partial_result_without_forcing(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/branch-error"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)
    real_run_git = gc_git._run_git

    def fail_branch_delete(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args[:2] == ["branch", "-d"]:
            return subprocess.CompletedProcess(
                ["git", *args],
                1,
                stdout=b"",
                stderr=b"simulated refusal",
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", fail_branch_delete)

    result = collect_git_worktree(decision, dry_run=False)

    assert result["ok"] is False
    assert result["action"] == "partial"
    assert result["removed_worktree"] is True
    assert result["branch_deleted"] is False
    assert result["branch_kept"] is True
    assert result["errors"] == ["branch_delete_failed"]
    assert not worktree.exists()
    assert _branch_exists(repo, branch)


def test_second_collection_pass_is_an_idempotent_noop(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/idempotent"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)

    first = collect_git_worktree(decision, dry_run=False)
    second = collect_git_worktree(decision, dry_run=False)

    assert first["ok"] is True
    assert first["removed_worktree"] is True
    assert first["branch_deleted"] is True
    assert second["ok"] is True
    assert second["refused"] is False
    assert second["idempotent"] is True
    assert second["action"] == "already_absent"
    assert second["removed_worktree"] is False
    assert second["branch_deleted"] is False
    assert second["branch_kept"] is False
    assert second["errors"] == []
    assert not worktree.exists()
    assert not _listed(repo, worktree)
    assert not _branch_exists(repo, branch)


def test_idempotent_result_reports_deleted_branch_even_if_target_disappears(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    branch = "gc/idempotent-target-gone"
    worktree = add_worktree(case, tmp_path, branch)
    decision = classify_git_worktree(worktree, branch, repo)
    first = collect_git_worktree(decision, dry_run=False)
    assert first["branch_deleted"] is True
    _git(repo, "update-ref", "-d", "refs/remotes/origin/master")

    second = collect_git_worktree(decision, dry_run=False)

    assert second["ok"] is True
    assert second["idempotent"] is True
    assert second["action"] == "already_absent"
    assert second["branch_kept"] is False
