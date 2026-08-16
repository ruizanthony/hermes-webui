"""A missing WebUI sidecar must not be reported as a worktree ownership refusal.

Reproduction (2026-08-16): a WebUI session whose transcript lives in state.db
but whose sidecar file was lost (process restart during a deploy) became
permanently unusable. POST /api/chat/start and POST /api/terminal/start each
run their own inline pre-flight ownership probe that calls
``get_session(sid, metadata_only=True)``.  That raises ``KeyError`` for a
missing sidecar, the bare ``except Exception`` swallows it, and the handler
answers ``409 Worktree write refused: '<sid>'`` -- naming the session id as if
it were an ownership error.

Two independent defects:

1. **Wrong verdict.** ``/api/chat/start`` already knows how to recover this
   exact case: ``_handle_chat_start`` catches ``KeyError`` and calls
   ``_claim_or_synthesize_cli_session`` to materialize the sidecar from
   state.db.  The pre-flight probe fires *before* that recovery and turns a
   recoverable state into a hard 409, so the recovery path is dead code for
   every session that actually needs it.
2. **Wrong class.** The canonical chokepoint
   ``_guard_request_worktree_ownership`` (used by rename/archive/etc.) gets
   this right -- it returns ``True`` on ``KeyError`` and lets the handler
   decide.  These two call sites are divergent copies of that guard.

The tests below assert the observable HTTP behavior of both endpoints.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
def linked(tmp_path):
    """A real linked Git worktree (the only workspace shape the guard inspects)."""
    repo = tmp_path / "repo"
    wt = tmp_path / "linked"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("x\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "worktree", "add", "-q", "-b", "linked", str(wt))
    return repo, wt


def _capture_post(monkeypatch, body):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: captured.update(
            payload={"error": message}, status=status
        )
        or True,
    )
    return captured


def _missing_sidecar_session(monkeypatch):
    """Make get_session() behave exactly as it does for a lost sidecar."""
    import api.routes as routes

    def _raise_keyerror(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", _raise_keyerror)


def test_chat_start_missing_sidecar_reaches_recovery_instead_of_409(monkeypatch):
    """The pre-flight probe must not convert a missing sidecar into a 409."""
    import api.routes as routes

    _missing_sidecar_session(monkeypatch)
    captured = _capture_post(monkeypatch, {"session_id": "20260816_210523_091383",
                                           "message": "continue"})

    reached = {}

    def _fake_handle_chat_start(handler, body, diag=None):
        reached["session_id"] = body.get("session_id")
        return True

    monkeypatch.setattr(routes, "_handle_chat_start", _fake_handle_chat_start)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/chat/start")) is True

    # The handler owns the missing-sidecar decision (it can materialize from
    # state.db via _claim_or_synthesize_cli_session).  The guard must not
    # short-circuit it.
    assert reached.get("session_id") == "20260816_210523_091383"
    assert captured.get("status") != 409
    assert "Worktree write refused" not in str(captured.get("payload", ""))


def test_terminal_start_missing_sidecar_reaches_handler_instead_of_409(monkeypatch):
    """Sibling call site: /api/terminal/start had the identical defect."""
    import api.routes as routes

    _missing_sidecar_session(monkeypatch)
    captured = _capture_post(monkeypatch, {"session_id": "20260816_210523_091383"})

    reached = {}

    def _fake_handle_terminal_start(handler, body):
        reached["session_id"] = body.get("session_id")
        return True

    monkeypatch.setattr(routes, "_handle_terminal_start", _fake_handle_terminal_start)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/terminal/start")) is True

    assert reached.get("session_id") == "20260816_210523_091383"
    assert captured.get("status") != 409
    assert "Worktree terminal refused" not in str(captured.get("payload", ""))


def test_chat_start_still_refuses_a_real_ownership_violation(linked, tmp_path, monkeypatch):
    """Fail-closed is preserved: a genuinely unowned linked worktree still 409s."""
    import api.routes as routes
    import api.worktree_authority as authority_module
    from api.worktree_authority import WorktreeAuthority

    _repo, wt = linked
    authority = WorktreeAuthority(tmp_path / "claims.sqlite3")
    # Claimed by a DIFFERENT session -> a real ownership violation.
    authority.claim(wt, "another-session")
    monkeypatch.setattr(authority_module, "default_authority", lambda: authority)

    session = SimpleNamespace(session_id="intruder", workspace=str(wt))
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)

    captured = _capture_post(monkeypatch, {"session_id": "intruder", "message": "hi"})

    called = {"handler": False}

    def _fake_handle_chat_start(handler, body, diag=None):
        called["handler"] = True
        return True

    monkeypatch.setattr(routes, "_handle_chat_start", _fake_handle_chat_start)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/chat/start")) is True

    assert called["handler"] is False
    assert captured["status"] == 409
    assert "Worktree write refused" in captured["payload"]["error"]
    assert "owned by another session" in captured["payload"]["error"]


def test_terminal_start_still_refuses_a_real_ownership_violation(linked, tmp_path, monkeypatch):
    """Sibling fail-closed check for the terminal endpoint."""
    import api.routes as routes
    import api.worktree_authority as authority_module
    from api.worktree_authority import WorktreeAuthority

    _repo, wt = linked
    authority = WorktreeAuthority(tmp_path / "claims.sqlite3")
    authority.claim(wt, "another-session")
    monkeypatch.setattr(authority_module, "default_authority", lambda: authority)

    session = SimpleNamespace(session_id="intruder", workspace=str(wt))
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)

    captured = _capture_post(monkeypatch, {"session_id": "intruder"})

    called = {"handler": False}

    def _fake_handle_terminal_start(handler, body):
        called["handler"] = True
        return True

    monkeypatch.setattr(routes, "_handle_terminal_start", _fake_handle_terminal_start)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/terminal/start")) is True

    assert called["handler"] is False
    assert captured["status"] == 409
    # After the fix both endpoints answer through the canonical chokepoint, so
    # the refusal wording is the shared one.
    assert "Worktree write refused" in captured["payload"]["error"]
    assert "owned by another session" in captured["payload"]["error"]
