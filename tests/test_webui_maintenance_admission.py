"""Fail-closed admission tests for autonomous WebUI turns during maintenance."""

from __future__ import annotations

from contextlib import contextmanager
import sys
import threading
import types

import pytest


def test_start_session_turn_returns_retryable_409_before_impl(monkeypatch):
    from api import maintenance_gate, routes

    @contextmanager
    def blocked_gate():
        raise maintenance_gate.WebUIMaintenanceInProgress("test maintenance")
        yield  # pragma: no cover

    called = []
    monkeypatch.setattr(maintenance_gate, "webui_server_turn_admission", blocked_gate)
    monkeypatch.setattr(
        routes,
        "_start_session_turn_impl",
        lambda *_args, **_kwargs: called.append(True),
    )

    response = routes.start_session_turn("session-a", "durable wakeup")

    assert response["_status"] == 409
    assert response["error"] == "maintenance_in_progress"
    assert called == []


def test_start_session_turn_holds_gate_through_impl(monkeypatch):
    from api import maintenance_gate, routes

    state = {"held": False}

    @contextmanager
    def recording_gate():
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    def admitted_impl(*_args, **_kwargs):
        assert state["held"] is True
        return {"_status": 200, "stream_id": "stream-a"}

    monkeypatch.setattr(maintenance_gate, "webui_server_turn_admission", recording_gate)
    monkeypatch.setattr(routes, "_start_session_turn_impl", admitted_impl)

    response = routes.start_session_turn("session-a", "durable wakeup")

    assert response["stream_id"] == "stream-a"
    assert state["held"] is False


def test_transferred_lease_remains_held_after_admission_context(monkeypatch):
    from api import maintenance_gate

    state = {"held": False}

    @contextmanager
    def fake_lease(platform):
        assert platform == "webui"
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    class FakeMaintenanceInProgress(RuntimeError):
        pass

    fake_activity = types.ModuleType("hermes_cli.maintenance_activity")
    fake_activity.cli_tui_turn_lease = fake_lease
    fake_activity.MaintenanceInProgress = FakeMaintenanceInProgress
    monkeypatch.setitem(sys.modules, "hermes_cli.maintenance_activity", fake_activity)
    monkeypatch.setattr(maintenance_gate, "external_drain_requested", lambda: False)

    with maintenance_gate.webui_server_turn_admission() as handoff:
        assert state["held"] is True
        handoff.transfer_to_worker()

    assert state["held"] is True
    handoff.release()
    assert state["held"] is False


def test_start_session_turn_passes_handoff_to_start_impl(monkeypatch):
    from api import maintenance_gate, routes

    class FakeHandoff:
        def __init__(self):
            self.transferred = False

        def transfer_to_worker(self):
            self.transferred = True

    handoff = FakeHandoff()

    @contextmanager
    def fake_gate():
        yield handoff

    def fake_start(*_args, **kwargs):
        assert kwargs["_maintenance_handoff"] is handoff
        handoff.transfer_to_worker()
        return {"_status": 202, "stream_id": "stream-1"}

    monkeypatch.setattr(maintenance_gate, "webui_server_turn_admission", fake_gate)
    monkeypatch.setattr(routes, "_start_session_turn_impl", fake_start)

    result = routes.start_session_turn("session-1", "continue")

    assert result == {"_status": 202, "stream_id": "stream-1"}
    assert handoff.transferred is True


def test_worker_thread_holds_handoff_until_target_finishes():
    from api import routes

    entered = threading.Event()
    finish = threading.Event()

    class FakeHandoff:
        def __init__(self):
            self.transferred = False
            self.released = False

        def transfer_to_worker(self):
            self.transferred = True

        def release(self):
            self.released = True

    handoff = FakeHandoff()

    def worker():
        entered.set()
        assert finish.wait(timeout=2)

    thread = routes._start_worker_thread_with_maintenance_handoff(
        worker,
        args=(),
        kwargs={},
        maintenance_handoff=handoff,
    )

    assert entered.wait(timeout=2)
    assert handoff.transferred is True
    assert handoff.released is False
    finish.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert handoff.released is True


def test_worker_thread_constructor_failure_does_not_transfer_handoff(monkeypatch):
    from api import routes

    class FakeHandoff:
        def __init__(self):
            self.transferred = False
            self.released = False

        def transfer_to_worker(self):
            self.transferred = True

        def release(self):
            self.released = True

    handoff = FakeHandoff()

    def fail_constructor(**_kwargs):
        raise RuntimeError("thread constructor failed")

    monkeypatch.setattr(routes.threading, "Thread", fail_constructor)

    with pytest.raises(RuntimeError, match="thread constructor failed"):
        routes._start_worker_thread_with_maintenance_handoff(
            lambda: None,
            args=(),
            kwargs={},
            maintenance_handoff=handoff,
        )

    assert handoff.transferred is False
    assert handoff.released is False
