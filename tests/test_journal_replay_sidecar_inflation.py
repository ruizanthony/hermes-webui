"""Regression tests — runaway session sidecar growth from non-deduped journal replays.

Root cause of the multi-GB session sidecars (e.g. a 1.4 GB
``20260814_020918_b31be9.json`` holding ~5M messages for a few hundred real
turns): the pending-turn recovery paths in ``api/models.py`` called
``_recover_journaled_output_and_terminal_error()`` WITHOUT
``dedupe_existing=True``. The parameter defaulted to ``False``, so every
re-entry into recovery for the same dead stream (state.db resync on load,
failed save retried on the next load, lazy retry hook) re-appended the full
journal output as brand-new messages. Repeated over days on a busy session,
the sidecar grew geometrically until loads timed out — which is exactly the
"long conversations are not faster to load" complaint.

Fix under test:
1. ``dedupe_existing`` now defaults to ``True`` on both
   ``_recover_journaled_output_and_terminal_error`` and
   ``_append_journaled_partial_output`` — replay is idempotent by default.
2. An anti-amplification guard refuses to replay a journal when the session
   already contains at least as many recovered messages for that stream as
   the journal has events (a bound no legitimate single replay can exceed),
   so previously-inflated sessions stop growing even before repair.
"""
from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import (
    Session,
    _append_journaled_partial_output,
    _recover_journaled_output_and_terminal_error,
)
from api.run_journal import append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    # Mirror tests/test_issue3875_recovery_anchor_dedup.py — isolate HERMES_HOME
    # so Session.save() + run-journal writes land in a throwaway sandbox.
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _text_journal(session_id: str, stream_id: str) -> None:
    append_run_event(session_id, stream_id, "token", {"text": "Bonjour "})
    append_run_event(session_id, stream_id, "token", {"text": "Anthony"})
    append_run_event(session_id, stream_id, "done", {})


def _count_recovered(session, stream_id: str) -> int:
    return sum(
        1
        for m in session.messages
        if isinstance(m, dict) and m.get("_recovered_stream_id") == stream_id
    )


def test_default_replay_is_idempotent(hermes_home):
    """The production call sites (pending-turn recovery in models.py) invoke
    ``_recover_journaled_output_and_terminal_error`` without any dedupe kwarg.
    That default path must NOT append duplicates when recovery re-runs for the
    same stream — this was the sidecar-inflation bug."""
    sid = "issue_sidecar_dupe"
    stream_id = "stream-D"
    _text_journal(sid, stream_id)
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])

    # Exactly how the recovery paths call it: no dedupe_existing kwarg.
    for _ in range(15):
        _recover_journaled_output_and_terminal_error(s, stream_id)

    assert _count_recovered(s, stream_id) == 1, (
        f"default-arg replay must be idempotent; got "
        f"{_count_recovered(s, stream_id)} copies of the recovered turn "
        f"(the multi-GB sidecar inflation bug)"
    )
    bodies = [
        m.get("content")
        for m in s.messages
        if isinstance(m, dict) and m.get("_recovered_from_run_journal")
    ]
    assert bodies.count("Bonjour Anthony") == 1


def test_append_journaled_partial_output_defaults_to_dedupe(hermes_home):
    """The low-level helper's default must match: idempotent without kwargs."""
    sid = "issue_sidecar_lowlevel"
    stream_id = "stream-L"
    _text_journal(sid, stream_id)
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])

    for _ in range(10):
        _append_journaled_partial_output(s, stream_id)

    assert _count_recovered(s, stream_id) == 1


def test_amplification_guard_stops_replay_on_inflated_session(hermes_home):
    """A session already inflated by the historical bug (more recovered
    messages for a stream than the journal has events) must refuse further
    replay outright — even a buggy caller passing dedupe_existing=False can
    no longer make it grow."""
    sid = "issue_sidecar_guard"
    stream_id = "stream-G"
    _text_journal(sid, stream_id)  # 3 events
    inflated = [{"role": "user", "content": "go"}]
    for i in range(50):  # simulate prior runaway duplication
        inflated.append(
            {
                "role": "assistant",
                "content": "Bonjour Anthony",
                "timestamp": 1000 + i,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            }
        )
    s = Session(session_id=sid, title="repro", messages=inflated)

    before = len(s.messages)
    result = _append_journaled_partial_output(s, stream_id, dedupe_existing=False)

    assert result is False
    assert len(s.messages) == before, (
        "amplification guard must freeze an already-inflated session, "
        f"but message count went {before} -> {len(s.messages)}"
    )


def test_guard_does_not_block_first_recovery(hermes_home):
    """The guard must not break legitimate first-time recovery."""
    sid = "issue_sidecar_first"
    stream_id = "stream-F"
    _text_journal(sid, stream_id)
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])

    assert _append_journaled_partial_output(s, stream_id) is True
    assert _count_recovered(s, stream_id) == 1
    assert any(
        isinstance(m, dict) and m.get("content") == "Bonjour Anthony"
        for m in s.messages
    )


def test_context_brief_banner_button_opens_container():
    """Frontend: the 'Brief contexte' banner button must open the panel
    CONTAINER (mobile drawer / collapsed desktop rail) before switching to the
    context panel — a bare switchPanel('context') activates an invisible panel
    and the tap appears to do nothing."""
    from pathlib import Path

    panels = Path(__file__).resolve().parent.parent.joinpath(
        "static", "panels.js"
    ).read_text(encoding="utf-8")
    start = panels.index("function _contextBriefBannerNode")
    end = panels.index("\n}", start)
    body = panels[start:end]
    assert "toggleMobileSidebar" in body, (
        "banner button must open the mobile sidebar drawer before switchPanel"
    )
    assert "expandSidebar" in body, (
        "banner button must expand the collapsed desktop rail before switchPanel"
    )
    assert "switchPanel" in body
