"""Startup recovery must never delay the WebUI listener."""

from __future__ import annotations

import threading


def test_startup_recovery_scheduler_is_non_blocking(monkeypatch):
    import server

    started = threading.Event()
    release = threading.Event()

    def blocked_recovery():
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(server, "_run_startup_session_recovery", blocked_recovery)

    timer = server._schedule_startup_session_recovery(delay=0.01)
    try:
        assert timer.daemon is True
        assert timer.name == "webui-startup-session-recovery"
        assert started.wait(timeout=1)
        assert timer.is_alive(), "the caller must return while recovery is still running"
    finally:
        release.set()
        timer.join(timeout=2)

    assert not timer.is_alive()


def test_sidebar_defers_stale_stream_cleanup_for_oversized_sidecar(monkeypatch):
    import api.routes as routes

    calls = []
    monkeypatch.setattr(
        routes,
        "_sidecar_file_exceeds_threshold",
        lambda session_id, threshold: calls.append((session_id, threshold)) or True,
    )

    def fail_if_loaded(*_args, **_kwargs):
        raise AssertionError("oversized sidecar must not be fully loaded by /api/sessions")

    monkeypatch.setattr(routes, "get_session", fail_if_loaded)

    changed = routes._reconcile_stale_stream_state_for_session_rows(
        [
            {
                "session_id": "s_oversized_stale_stream",
                "active_stream_id": "stream_stale",
                "is_streaming": False,
            }
        ]
    )

    assert changed is False
    assert calls == [("s_oversized_stale_stream", 128 * 1024 * 1024)]
