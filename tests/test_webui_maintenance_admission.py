"""Fail-closed admission tests for autonomous WebUI turns during maintenance."""

from __future__ import annotations

from contextlib import contextmanager


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

