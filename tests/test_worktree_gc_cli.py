import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.worktree_gc_inventory import write_report_atomic
from scripts import worktree_gc


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """These unit tests do not need the repository's HTTP server fixture."""


class FakeBackend:
    def __init__(self, results=None, events=None):
        self.results = list(results or [])
        self.collect_calls = []
        self.events = events

    def collect_git_worktree(self, decision, *, dry_run):
        self.collect_calls.append((decision, dry_run))
        if self.events is not None:
            self.events.append(("collect", decision))
        if self.results:
            return self.results.pop(0)
        return {"ok": True, "removed": True}


def _report(repo, candidates=(), *, blocking=False, allowed=True):
    return {
        "schema_version": 1,
        "generated_at": "2026-07-23T12:00:00+00:00",
        "profile": "default",
        "repo": str(repo),
        "target_ref": "origin/master",
        "min_age_days": 7,
        "collection_allowed": allowed,
        "global_reasons": [] if allowed else ["health_unavailable"],
        "health": {"reachable": allowed, "active_runs": 0 if allowed else None},
        "process_scan": {
            "available": True,
            "complete": True,
            "process_count": 0,
            "unreadable_count": 0,
            "disappeared_count": 0,
        },
        "session_scan": {
            "sidecars_scanned": len(candidates),
            "errors": 0,
            "error_kinds": [],
        },
        "candidates": list(candidates),
        "counts": {
            "candidates": len(candidates),
            "eligible": sum(bool(item.get("eligible")) for item in candidates),
            "blocking": int(blocking),
            "uncertain": int(blocking),
            "active": 0,
        },
        "has_blocking_anomalies": blocking,
    }


def _eligible(path, repo):
    return {
        "session_id": Path(path).name,
        "session_ids": [Path(path).name],
        "profile": "default",
        "worktree_path": str(path),
        "worktree_branch": f"hermes/{Path(path).name}",
        "worktree_repo_root": str(repo),
        "worktree_created_at": 1_700_000_000.0,
        "age_days": 30.0,
        "age_source": "worktree_created_at",
        "verdict": "REMOVE_ANCESTOR",
        "eligible": True,
        "reasons": ["branch_is_ancestor"],
    }


def _valid_revalidation(**_kwargs):
    return SimpleNamespace(
        allowed=True,
        global_guard=False,
        reason=None,
        global_reasons=(),
        candidate_reasons=(),
    )


def test_cli_defaults_to_dry_run_and_emits_valid_json(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    state_dir = tmp_path / "hermes-home" / "webui"
    report_path = tmp_path / "report.json"
    repo.mkdir()
    calls = []

    def fake_audit(**kwargs):
        calls.append(kwargs)
        return _report(repo)

    backend = FakeBackend()
    stdout = io.StringIO()
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_WEBUI_PORT", "9898")

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--report-path",
            str(report_path),
            "--json",
        ],
        git_backend=backend,
        audit_fn=fake_audit,
        stdout=stdout,
    )

    assert return_code == 0
    assert backend.collect_calls == []
    assert calls[0]["profile"] == "default"
    assert calls[0]["min_age_days"] == 7
    assert calls[0]["target_ref"] == "origin/master"
    assert calls[0]["state_dir"] == state_dir
    assert calls[0]["health_url"] == "http://127.0.0.1:9898/health"
    assert json.loads(stdout.getvalue())["mode"] == "dry-run"
    assert json.loads(report_path.read_text(encoding="utf-8"))["mode"] == "dry-run"


def test_help_does_not_load_the_deferred_git_backend(monkeypatch):
    monkeypatch.setattr(
        worktree_gc,
        "load_git_backend",
        lambda: pytest.fail("backend import must remain deferred"),
    )

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(["--help"])

    assert exc.value.code == 0


def test_collect_is_explicit_and_sorts_deepest_paths_first(tmp_path):
    repo = tmp_path / "repo"
    parent = tmp_path / "worktrees" / "parent"
    child = parent / "child"
    repo.mkdir()
    child.mkdir(parents=True)
    report_path = tmp_path / "report.json"
    parent_decision = object()
    child_decision = object()

    def fake_audit(**kwargs):
        kwargs["decision_sink"].update(
            {
                str(parent): parent_decision,
                str(child): child_decision,
            }
        )
        return _report(
            repo,
            [_eligible(parent, repo), _eligible(child, repo)],
        )

    events = []
    backend = FakeBackend(events=events)

    def revalidate(**kwargs):
        events.append(("revalidate", kwargs["audited_candidate"]["worktree_path"]))
        return _valid_revalidation()

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
            "--health-url",
            "http://127.0.0.1:8787/health",
            "--report-path",
            str(report_path),
            "--collect",
        ],
        git_backend=backend,
        audit_fn=fake_audit,
        revalidate_fn=revalidate,
        stdout=io.StringIO(),
    )

    assert return_code == 0
    assert backend.collect_calls == [
        (child_decision, False),
        (parent_decision, False),
    ]
    assert events == [
        ("revalidate", str(child)),
        ("collect", child_decision),
        ("revalidate", str(parent)),
        ("collect", parent_decision),
    ]
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["mode"] == "collect"
    assert [item["collection"]["status"] for item in persisted["candidates"]] == [
        "collected",
        "collected",
    ]


def test_collection_failure_stops_remaining_mutations_and_returns_three(tmp_path):
    repo = tmp_path / "repo"
    first = tmp_path / "worktrees" / "first" / "nested"
    second = tmp_path / "worktrees" / "second"
    repo.mkdir()
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    report_path = tmp_path / "report.json"
    first_decision = object()
    second_decision = object()

    def fake_audit(**kwargs):
        kwargs["decision_sink"].update(
            {
                str(first): first_decision,
                str(second): second_decision,
            }
        )
        return _report(repo, [_eligible(first, repo), _eligible(second, repo)])

    backend = FakeBackend(results=[{"ok": False, "error": "revalidation failed"}])

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--report-path",
            str(report_path),
            "--collect",
        ],
        git_backend=backend,
        audit_fn=fake_audit,
        revalidate_fn=_valid_revalidation,
        stdout=io.StringIO(),
    )

    assert return_code == 3
    assert backend.collect_calls == [(first_decision, False)]
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    by_path = {
        item["worktree_path"]: item["collection"]
        for item in persisted["candidates"]
    }
    assert by_path[str(first)]["status"] == "failed"
    assert by_path[str(second)]["status"] == "skipped"


def test_collect_request_blocked_by_global_guard_is_reported_without_mutation(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    report_path = tmp_path / "report.json"
    backend = FakeBackend()

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--report-path",
            str(report_path),
            "--collect",
        ],
        git_backend=backend,
        audit_fn=lambda **_kwargs: _report(repo, allowed=False),
        stdout=io.StringIO(),
    )

    assert return_code == 2
    assert backend.collect_calls == []
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["collection"] == {
        "attempted": 0,
        "collected": 0,
        "failed": 0,
        "skipped": 0,
        "blocked": True,
        "reasons": ["health_unavailable"],
    }


@pytest.mark.parametrize(
    ("blocking", "allowed", "expected"),
    [
        (False, True, 0),
        (True, True, 2),
        (False, False, 2),
    ],
)
def test_cli_audit_exit_codes(tmp_path, blocking, allowed, expected):
    repo = tmp_path / "repo"
    repo.mkdir()

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--report-path",
            str(tmp_path / "report.json"),
        ],
        git_backend=FakeBackend(),
        audit_fn=lambda **_kwargs: _report(
            repo,
            blocking=blocking,
            allowed=allowed,
        ),
        stdout=io.StringIO(),
    )

    assert return_code == expected


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_cli_rejects_invalid_min_age(value, tmp_path):
    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            [
                "--repo",
                str(tmp_path),
                "--min-age-days",
                value,
            ],
            git_backend=FakeBackend(),
        )

    assert exc.value.code == 2


def test_cli_rejects_collect_with_explicit_dry_run(tmp_path):
    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            [
                "--repo",
                str(tmp_path),
                "--dry-run",
                "--collect",
            ],
            git_backend=FakeBackend(),
        )

    assert exc.value.code == 2


def test_cli_requires_repo():
    with pytest.raises(SystemExit) as exc:
        worktree_gc.main([], git_backend=FakeBackend())

    assert exc.value.code == 2


def test_report_write_replaces_atomically_in_same_directory(tmp_path, monkeypatch):
    report_path = tmp_path / "reports" / "latest.json"
    report_path.parent.mkdir()
    report_path.write_text('{"old": true}\n', encoding="utf-8")
    real_replace = os.replace
    replacements = []

    def observing_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append((source_path, destination_path))
        assert source_path.parent == report_path.parent
        assert json.loads(source_path.read_text(encoding="utf-8")) == {
            "new": True
        }
        assert json.loads(report_path.read_text(encoding="utf-8")) == {
            "old": True
        }
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", observing_replace)

    write_report_atomic({"new": True}, report_path)

    assert replacements
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"new": True}
    assert list(report_path.parent.glob(f".{report_path.name}.*")) == []


def test_default_report_path_prefers_xdg_user_state_outside_repository(
    tmp_path,
    monkeypatch,
):
    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))

    report_path = worktree_gc.default_report_path()
    repo_root = Path(worktree_gc.__file__).resolve().parents[1]

    assert report_path == (
        xdg_state / "hermes-webui" / "worktree-gc" / "report.json"
    )
    with pytest.raises(ValueError):
        report_path.resolve().relative_to(repo_root)


def test_candidate_revalidation_refusal_skips_safely_and_returns_two(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    blocked = tmp_path / "worktrees" / "blocked" / "nested"
    collectable = tmp_path / "worktrees" / "collectable"
    repo.mkdir()
    blocked.mkdir(parents=True)
    collectable.mkdir(parents=True)
    report_path = tmp_path / "report.json"
    blocked_decision = object()
    collectable_decision = object()
    events = []
    snapshots = []
    real_write_report = write_report_atomic

    def fake_audit(**kwargs):
        kwargs["decision_sink"].update(
            {
                str(blocked): blocked_decision,
                str(collectable): collectable_decision,
            }
        )
        return _report(
            repo,
            [_eligible(blocked, repo), _eligible(collectable, repo)],
        )

    def revalidate(**kwargs):
        path = kwargs["audited_candidate"]["worktree_path"]
        events.append(("revalidate", path))
        if path == str(blocked):
            return SimpleNamespace(
                allowed=False,
                global_guard=False,
                reason="candidate_runtime_guard",
                global_reasons=(),
                candidate_reasons=("session_not_archived",),
            )
        return _valid_revalidation()

    def observing_write(report, path):
        snapshots.append(json.loads(json.dumps(report)))
        real_write_report(report, path)

    monkeypatch.setattr(worktree_gc, "write_report_atomic", observing_write)
    backend = FakeBackend(events=events)

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
            "--report-path",
            str(report_path),
            "--collect",
        ],
        git_backend=backend,
        audit_fn=fake_audit,
        revalidate_fn=revalidate,
        stdout=io.StringIO(),
    )

    assert return_code == 2
    assert backend.collect_calls == [(collectable_decision, False)]
    assert events == [
        ("revalidate", str(blocked)),
        ("revalidate", str(collectable)),
        ("collect", collectable_decision),
    ]
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    by_path = {item["worktree_path"]: item for item in persisted["candidates"]}
    assert by_path[str(blocked)]["collection"] == {
        "status": "skipped",
        "reason": "candidate_runtime_guard",
        "guard_reasons": ["session_not_archived"],
    }
    assert by_path[str(collectable)]["collection"]["status"] == "collected"
    assert persisted["has_blocking_anomalies"] is True
    assert persisted["collection"]["failed"] == 0
    assert any(
        any(
            item.get("collection", {}).get("status") == "skipped"
            for item in snapshot["candidates"]
        )
        for snapshot in snapshots
    )


def test_global_runtime_guard_stops_and_skips_all_remaining_mutations(tmp_path):
    repo = tmp_path / "repo"
    first = tmp_path / "worktrees" / "first" / "deep"
    second = tmp_path / "worktrees" / "second"
    third = tmp_path / "third"
    repo.mkdir()
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    third.mkdir()
    report_path = tmp_path / "report.json"
    decisions = {
        str(first): object(),
        str(second): object(),
        str(third): object(),
    }
    revalidation_calls = []

    def fake_audit(**kwargs):
        kwargs["decision_sink"].update(decisions)
        return _report(
            repo,
            [
                _eligible(first, repo),
                _eligible(second, repo),
                _eligible(third, repo),
            ],
        )

    def revalidate(**kwargs):
        path = kwargs["audited_candidate"]["worktree_path"]
        revalidation_calls.append(path)
        if path == str(second):
            return SimpleNamespace(
                allowed=False,
                global_guard=True,
                reason="global_runtime_guard",
                global_reasons=("active_runs",),
                candidate_reasons=(),
            )
        return _valid_revalidation()

    backend = FakeBackend()
    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--report-path",
            str(report_path),
            "--collect",
        ],
        git_backend=backend,
        audit_fn=fake_audit,
        revalidate_fn=revalidate,
        stdout=io.StringIO(),
    )

    assert return_code == 2
    assert backend.collect_calls == [(decisions[str(first)], False)]
    assert revalidation_calls == [str(first), str(second)]
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    by_path = {item["worktree_path"]: item for item in persisted["candidates"]}
    assert by_path[str(first)]["collection"]["status"] == "collected"
    for path in (second, third):
        assert by_path[str(path)]["collection"] == {
            "status": "skipped",
            "reason": "global_runtime_guard",
            "guard_reasons": ["active_runs"],
        }
    assert persisted["collection_allowed"] is False
    assert "global_runtime_guard" in persisted["global_reasons"]
    assert persisted["collection"]["failed"] == 0
