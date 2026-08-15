"""Regression coverage for post-squash context coherence.

Incident (2026-08-15, session 20260814_161032_b73158): after a manual squash
compacted the sidecar to one summary message, the next turn rebuilt the agent,
and hermes-agent's compression-rotation recovery
(`recover_rotated_compression_session`) read the state.db sessions row —
still marked ``end_reason='compression'`` — followed the continuation chain
to the live tip, and REPLACED the squashed history with the archived lineage
(~326 messages, ~212K tokens). Preflight then fired "Compressing context"
at 78% while the gauge showed the stale sidecar count (79,814 = 29%).

This file pins the fix, in three parts:

1. ``detach_state_db_compression_lineage`` reopens a compression-rotated
   state.db row after a verified squash so the agent's recovery treats the
   squashed session as a standalone root and never re-injects the lineage.
2. The squash resets the gauge-facing counters (``last_prompt_tokens``,
   ``post_compression_context_tokens_estimate``) while preserving billing
   counters, and the streaming path publishes an authoritative usage
   snapshot before the agent's preflight can emit "Compressing context".
3. The usage payload carries the EFFECTIVE compression threshold percent,
   the CONFIGURED percent, and a floor-applied flag so the UI can explain
   the automatic 75% small-window floor instead of implying 45/50% applies.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]


def _ensure_agent_on_path() -> bool:
    """Make hermes-agent importable (mirrors conftest discovery)."""
    if importlib.util.find_spec("hermes_state") is not None:
        return True
    candidates = [
        os.getenv("HERMES_WEBUI_AGENT_DIR", ""),
        "/usr/local/lib/hermes-agent",
        str(Path.home() / ".hermes" / "hermes-agent"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "hermes_state.py").exists():
            sys.path.insert(0, str(Path(candidate)))
            return importlib.util.find_spec("hermes_state") is not None
    return False


requires_hermes_state = pytest.mark.skipif(
    not _ensure_agent_on_path(),
    reason="hermes_state not importable (agent checkout not present)",
)


def _open_session_db(db_path: Path):
    from hermes_state import SessionDB  # type: ignore

    return SessionDB(db_path=db_path)


def _seed_rotated_lineage(db_path: Path, parent_sid: str, child_sid: str, child_rows: int = 40):
    """Create a compression-rotated parent with a unique live child tip."""
    db = _open_session_db(db_path)
    try:
        db.create_session(session_id=parent_sid, source="webui", model="test-model")
        # A few parent rows so the recovered history is non-trivial.
        db.append_messages_batch(parent_sid, [
            {"role": "user", "content": f"parent question {i}", "timestamp": 1000.0 + i}
            for i in range(4)
        ])
        holder = "pid=1:tid=1:agent=test:nonce=seed"
        assert db.try_acquire_compression_lock(parent_sid, holder) is True
        db.publish_compression_child(
            parent_session_id=parent_sid,
            child_session_id=child_sid,
            source="webui",
            messages=[
                {"role": "assistant", "content": "[CONTEXT COMPACTION] summary"},
            ],
            model="test-model",
            compression_lock_holder=holder,
        )
        db.append_messages_batch(child_sid, [
            {"role": "assistant", "content": f"tip row {i}", "timestamp": 2000.0 + i}
            for i in range(child_rows)
        ])
    finally:
        db.close()


class _DummyAgentForRecovery:
    def __init__(self, session_id, db):
        self.session_id = session_id
        self._session_db = db
        self._session_db_created = False
        self.context_compressor = None
        self._memory_manager = None
        self.platform = "webui"


# ── Part 1: state.db lineage detach ────────────────────────────────────────


@requires_hermes_state
def test_detach_reopens_compression_rotated_row(tmp_path):
    from api import session_squash

    db_path = tmp_path / "state.db"
    parent_sid = "20260814_161032_b73158"
    child_sid = "20260814_200309_ab511c"
    _seed_rotated_lineage(db_path, parent_sid, child_sid)

    # Baseline: the incident's re-injection reproduces before the detach.
    from agent.conversation_compression import (  # type: ignore
        _session_was_rotated_by_compression,
        recover_rotated_compression_session,
    )

    db = _open_session_db(db_path)
    try:
        assert _session_was_rotated_by_compression(db, parent_sid) is True
        recovered = recover_rotated_compression_session(
            _DummyAgentForRecovery(parent_sid, db)
        )
        assert recovered is not None and len(recovered) > 10
    finally:
        db.close()

    # The fix: detach the rotated lineage after a verified squash.
    assert session_squash.detach_state_db_compression_lineage(
        parent_sid, state_db_path=db_path
    ) is True

    db = _open_session_db(db_path)
    try:
        assert _session_was_rotated_by_compression(db, parent_sid) is False
        assert recover_rotated_compression_session(
            _DummyAgentForRecovery(parent_sid, db)
        ) is None
        # The child tip row is untouched: its lineage metadata and rows
        # remain exactly as published.
        child_row = db.get_session(child_sid)
        assert child_row is not None
        assert child_row.get("parent_session_id") == parent_sid
        assert child_row.get("end_reason") is None
        assert len(db.get_messages_as_conversation(child_sid)) > 10
        # The parent row is reopened (standalone root again).
        parent_row = db.get_session(parent_sid)
        assert parent_row.get("ended_at") is None
        assert parent_row.get("end_reason") is None
    finally:
        db.close()


@requires_hermes_state
def test_detach_is_idempotent_and_safe_for_live_rows(tmp_path):
    from api import session_squash

    db_path = tmp_path / "state.db"
    live_sid = "20260815_120000_liveok"
    db = _open_session_db(db_path)
    try:
        db.create_session(session_id=live_sid, source="webui", model="test-model")
    finally:
        db.close()

    # A live (non-rotation) row must never be touched.
    assert session_squash.detach_state_db_compression_lineage(
        live_sid, state_db_path=db_path
    ) is False

    # Missing session row / missing db are fail-open no-ops.
    assert session_squash.detach_state_db_compression_lineage(
        "20260815_120000_missing", state_db_path=db_path
    ) is False
    assert session_squash.detach_state_db_compression_lineage(
        live_sid, state_db_path=tmp_path / "absent.db"
    ) is False
    assert session_squash.detach_state_db_compression_lineage("", state_db_path=db_path) is False


# ── Part 2: squash job wiring ──────────────────────────────────────────────

SID = "20260801_120000_ab12cd"


def _make_session(tmp_path, *, messages=None, **overrides):
    sessions_dir = tmp_path / "webui" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{SID}.json"
    if messages is None:
        messages = [
            {"role": "user", "content": "première demande de test", "timestamp": 1000.0},
            {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 fait", "timestamp": 1001.0},
            {"role": "user", "content": "seconde demande", "timestamp": 1002.0},
            {"role": "assistant", "content": "réponse finale", "timestamp": 1003.0},
        ]
    sess = SimpleNamespace(
        session_id=SID,
        title="session de test",
        workspace="/tmp/ws",
        created_at=1000.0,
        updated_at=1003.0,
        messages=messages,
        context_messages=list(messages),
        tool_calls=[{"id": "t1"}],
        active_stream_id=None,
        active_checkpoint=None,
        pending_turn_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        parent_session_id=None,
        anchor_activity_scenes={},
        compression_anchor_visible_idx=None,
        compression_anchor_message_key=None,
        compression_anchor_summary=None,
        compression_anchor_mode=None,
        compaction_generation=None,
        truncation_watermark=None,
        truncation_boundary=None,
        read_only=False,
        profile="default",
        # Gauge-facing fields (stale pre-squash values in the incident).
        input_tokens=79814,
        output_tokens=60,
        estimated_cost=0.0,
        cache_read_tokens=28160,
        cache_write_tokens=0,
        last_prompt_tokens=79814,
        threshold_tokens=204000,
        context_length=272000,
        post_compression_context_tokens_estimate=90000,
        path=path,
    )
    for key, value in overrides.items():
        setattr(sess, key, value)

    def _save(**_kwargs):
        payload = {
            "session_id": SID,
            "title": sess.title,
            "workspace": sess.workspace,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
            "messages": sess.messages,
            "context_messages": sess.context_messages,
            "tool_calls": sess.tool_calls,
            "active_stream_id": sess.active_stream_id,
            "active_checkpoint": sess.active_checkpoint,
            "pending_turn_id": sess.pending_turn_id,
            "pending_user_message": sess.pending_user_message,
            "pending_attachments": sess.pending_attachments,
            "pending_started_at": sess.pending_started_at,
            "pending_user_source": sess.pending_user_source,
            "parent_session_id": sess.parent_session_id,
            "anchor_activity_scenes": sess.anchor_activity_scenes,
            "compression_anchor_visible_idx": sess.compression_anchor_visible_idx,
            "compression_anchor_message_key": sess.compression_anchor_message_key,
            "compression_anchor_summary": sess.compression_anchor_summary,
            "compression_anchor_mode": sess.compression_anchor_mode,
            "compaction_generation": sess.compaction_generation,
            "truncation_watermark": sess.truncation_watermark,
            "truncation_boundary": sess.truncation_boundary,
            "input_tokens": sess.input_tokens,
            "output_tokens": sess.output_tokens,
            "estimated_cost": sess.estimated_cost,
            "cache_read_tokens": sess.cache_read_tokens,
            "cache_write_tokens": sess.cache_write_tokens,
            "last_prompt_tokens": sess.last_prompt_tokens,
            "threshold_tokens": sess.threshold_tokens,
            "context_length": sess.context_length,
            "post_compression_context_tokens_estimate": (
                sess.post_compression_context_tokens_estimate
            ),
            "message_count": len(sess.messages or []),
            "read_only": sess.read_only,
            "profile": sess.profile,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sess.save = _save
    _save()
    return sess


class _DummyLock:
    def acquire(self, timeout=None):
        return True

    def release(self):
        return None


def _run_squash_job(sess, *, summary="synthèse fournie " * 40, detach_recorder=None):
    import api.models
    import api.routes
    import api.session_ops
    from api import session_squash

    def _detach(sid, **kwargs):
        if detach_recorder is not None:
            detach_recorder.append((sid, kwargs))
        return True

    with patch.object(api.models, "get_session", lambda sid, metadata_only=False: sess), \
         patch.object(api.session_ops, "_live_active_stream_id", lambda _s: None), \
         patch.object(api.routes, "_get_session_agent_lock", lambda _sid: _DummyLock()), \
         patch.object(api.routes, "_publish_session_list_changed", lambda *a, **k: None), \
         patch("api.config._evict_session_agent", lambda _sid: None), \
         patch.object(session_squash, "_generate_summary", lambda s, sid, provided: (provided.strip(), "provided")), \
         patch.object(session_squash, "detach_state_db_compression_lineage", _detach):
        job = session_squash.start_squash_job(SID, confirm_session_id=SID, summary=summary.strip())
        deadline = time.time() + 10
        while time.time() < deadline:
            snap = session_squash.squash_job_status(job["job_id"])
            if snap["status"] in ("done", "error"):
                return snap
            time.sleep(0.02)
    raise AssertionError("squash job did not finish")


def test_squash_detaches_state_db_lineage(tmp_path):
    sess = _make_session(tmp_path)
    calls = []
    snap = _run_squash_job(sess, detach_recorder=calls)
    assert snap["status"] == "done", snap.get("error")
    assert [sid for sid, _ in calls] == [SID]


def test_squash_resets_gauge_counters_and_keeps_billing(tmp_path):
    sess = _make_session(tmp_path)
    snap = _run_squash_job(sess)
    assert snap["status"] == "done", snap.get("error")

    # Billing/consumption history survives the squash (the /usage semantics).
    assert sess.input_tokens == 79814
    assert sess.output_tokens == 60
    assert sess.cache_read_tokens == 28160

    # Gauge-facing fields no longer advertise the stale pre-squash context:
    # the incident showed 79,814 tokens (29%) while preflight saw ~212K.
    assert sess.last_prompt_tokens != 79814
    assert 0 <= sess.last_prompt_tokens < 20000  # one summary message only
    assert sess.post_compression_context_tokens_estimate is None

    persisted = json.loads(sess.path.read_text(encoding="utf-8"))
    assert persisted["last_prompt_tokens"] == sess.last_prompt_tokens
    assert persisted["post_compression_context_tokens_estimate"] is None
    # The result advertises the fresh estimate so callers/UI can refresh.
    assert snap["result"]["after"].get("last_prompt_tokens_estimate") == sess.last_prompt_tokens


def test_squash_survives_detach_failure(tmp_path):
    """Fail-open: a state.db outage must not break a verified squash."""
    from api import session_squash

    sess = _make_session(tmp_path)
    with patch.object(
        session_squash, "detach_state_db_compression_lineage",
        side_effect=RuntimeError("state.db locked"),
    ):
        snap = _run_squash_job(sess)
    assert snap["status"] == "done", snap.get("error")
    assert snap["result"]["after"]["message_count"] == 1


# ── Part 3: authoritative usage publication + effective threshold ──────────


def _fake_compressor(**overrides):
    base = dict(
        context_length=272000,
        threshold_tokens=204000,
        last_prompt_tokens=212900,
        _config_threshold_percent=0.45,
        _base_threshold_percent=0.45,
        threshold_percent=0.75,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_compression_start_event_carries_authoritative_usage():
    from api import streaming

    agent = SimpleNamespace(context_compressor=_fake_compressor())
    payload = streaming._compression_start_event_payload("sid-1", agent)
    assert payload["session_id"] == "sid-1"
    assert payload["message"] == "Compressing context"
    usage = payload["usage"]
    assert usage["context_length"] == 272000
    assert usage["threshold_tokens"] == 204000
    assert usage["last_prompt_tokens"] == 212900
    # Effective-vs-configured threshold explanation (75% floor over 45%).
    assert usage["threshold_percent_configured"] == pytest.approx(0.45)
    assert usage["threshold_percent_effective"] == pytest.approx(0.75)
    assert usage["threshold_floor_applied"] is True


def test_compression_start_event_without_floor():
    from api import streaming

    agent = SimpleNamespace(context_compressor=_fake_compressor(
        context_length=900000,
        threshold_tokens=405000,
        threshold_percent=0.45,
    ))
    usage = streaming._compression_start_event_payload("sid-1", agent)["usage"]
    assert usage["threshold_floor_applied"] is False
    assert usage["threshold_percent_effective"] == pytest.approx(0.45)


def test_compression_start_event_no_compressor_is_empty():
    from api import streaming

    assert streaming._compression_start_event_payload("sid-1", SimpleNamespace())["usage"] == {}


def test_pre_turn_usage_snapshot_publishes_before_agent_run():
    """The gauge must be refreshed with the session's own numbers before
    run_conversation() — i.e. before the preflight can emit 'Compressing
    context' — so the displayed percentage matches the backend's input."""
    from api import streaming

    events = []

    def fake_put(event, data):
        events.append((event, data))

    session = SimpleNamespace(
        session_id="sid-1",
        context_length=272000,
        threshold_tokens=204000,
        last_prompt_tokens=1200,
        input_tokens=500,
        output_tokens=100,
        estimated_cost=0.0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        post_compression_context_tokens_estimate=None,
    )
    agent = SimpleNamespace(
        context_compressor=_fake_compressor(last_prompt_tokens=1200),
        session_prompt_tokens=500,
        session_completion_tokens=100,
        session_estimated_cost_usd=0.0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
    )
    streaming._publish_pre_turn_usage_snapshot("sid-1", session, agent, fake_put)

    assert events and events[0][0] == "metering"
    usage = events[0][1]["usage"]
    assert events[0][1]["session_id"] == "sid-1"
    assert usage["context_length"] == 272000
    assert usage["threshold_tokens"] == 204000
    assert usage["last_prompt_tokens"] == 1200
    assert usage["threshold_percent_effective"] == pytest.approx(0.75)
    assert usage["threshold_percent_configured"] == pytest.approx(0.45)
    assert usage["threshold_floor_applied"] is True


def test_turn_start_guard_detaches_legacy_squashed_lineage():
    """Legacy sessions squashed BEFORE this fix still carry a
    compression-ended state.db row; the turn-start guard repairs them."""
    from api import session_squash, streaming

    squashed = SimpleNamespace(
        session_id="sid-legacy",
        profile=None,
        compression_anchor_mode="manual",
        truncation_watermark=1234.0,
        truncation_boundary=1234.0,
        parent_session_id=None,
        messages=[{"role": "assistant", "content": "# Synthèse", "_squash_summary": True}],
    )
    calls = []
    with patch.object(
        session_squash, "detach_state_db_compression_lineage",
        lambda sid, **kw: calls.append((sid, kw)) or True,
    ):
        streaming._detach_rotated_lineage_for_squashed_session(squashed)
    assert [sid for sid, _ in calls] == ["sid-legacy"]

    # A legacy session squashed before this fix that ALREADY ran a turn
    # (watermark advanced past the boundary — the exact 2026-08-15 session
    # state) is still repaired: the durable _squash_summary marker on the
    # first message is the eligibility signal, not the watermark position.
    advanced = SimpleNamespace(
        session_id="sid-normal",
        profile=None,
        compression_anchor_mode="manual",
        truncation_watermark=2000.0,
        truncation_boundary=1234.0,
        parent_session_id=None,
        messages=[
            {"role": "assistant", "content": "# Synthèse", "_squash_summary": True},
            {"role": "user", "content": "tour post-squash", "timestamp": 2000.0},
        ],
    )
    calls.clear()
    with patch.object(
        session_squash, "detach_state_db_compression_lineage",
        lambda sid, **kw: calls.append((sid, kw)) or True,
    ):
        streaming._detach_rotated_lineage_for_squashed_session(advanced)
    assert [sid for sid, _ in calls] == ["sid-normal"]

    # A non-squashed session (no squash marker) is never touched.
    normal = SimpleNamespace(
        session_id="sid-normal-2",
        profile=None,
        compression_anchor_mode="automatic_tail",
        truncation_watermark=2000.0,
        truncation_boundary=1234.0,
        parent_session_id=None,
        messages=[{"role": "user", "content": "bonjour", "timestamp": 1000.0}],
    )
    calls.clear()
    with patch.object(
        session_squash, "detach_state_db_compression_lineage",
        lambda sid, **kw: calls.append((sid, kw)) or True,
    ):
        streaming._detach_rotated_lineage_for_squashed_session(normal)
    assert calls == []


def _corrupted_session(boundary=1000.0):
    """A squashed session whose context_messages got pre-squash rows
    re-injected (the 2026-08-15 incident shape)."""
    saved = []

    def _save(**_kwargs):
        saved.append(True)

    sess = SimpleNamespace(
        session_id="sid-corrupted",
        profile=None,
        compression_anchor_mode="manual",
        truncation_watermark=2000.0,
        truncation_boundary=boundary,
        parent_session_id=None,
        messages=[{"role": "assistant", "content": "# Synthèse", "_squash_summary": True}],
        context_messages=[
            {"role": "user", "content": "tour post-squash", "_ts": 2000.0},
            {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] s", "_ts": 2000.0},
            # Re-injected pre-squash rows: timestamps strictly below the boundary.
            {"role": "assistant", "content": "", "timestamp": 800.0},
            {"role": "tool", "content": "[read_file] x", "timestamp": 810.0},
            {"role": "assistant", "content": "", "timestamp": 950.0},
            # A post-squash row and an undated row must survive.
            {"role": "assistant", "content": "réponse", "_ts": 2100.0},
            {"role": "tool", "content": "undated row"},
        ],
        last_prompt_tokens=79814,
        post_compression_context_tokens_estimate=90000,
    )
    sess.save = _save
    return sess, saved


def test_repair_prunes_reinjected_pre_squash_rows():
    from api import session_squash

    sess, saved = _corrupted_session()
    assert session_squash.repair_reinjected_pre_squash_context(sess) is True
    contents = [m.get("content") for m in sess.context_messages]
    assert len(sess.context_messages) == 4  # 7 - 3 pre-squash rows
    assert "[read_file] x" not in contents
    assert all(
        session_squash._message_timestamp_float(m) is None
        or session_squash._message_timestamp_float(m) >= 1000.0
        for m in sess.context_messages
    )
    assert saved == [True]
    assert sess.post_compression_context_tokens_estimate is None
    assert sess.last_prompt_tokens != 79814
    assert sess.last_prompt_tokens > 0


def test_repair_is_idempotent_and_skips_clean_sessions():
    from api import session_squash

    sess, saved = _corrupted_session()
    assert session_squash.repair_reinjected_pre_squash_context(sess) is True
    # Second pass: nothing left to prune, no save.
    assert session_squash.repair_reinjected_pre_squash_context(sess) is False
    assert saved == [True]

    # A non-squashed session is never touched even with old-dated rows.
    normal = SimpleNamespace(
        session_id="sid-x",
        compression_anchor_mode="automatic_tail",
        truncation_boundary=1000.0,
        parent_session_id=None,
        messages=[{"role": "user", "content": "b", "timestamp": 500.0}],
        context_messages=[{"role": "user", "content": "b", "timestamp": 500.0}],
        last_prompt_tokens=10,
        post_compression_context_tokens_estimate=None,
    )
    normal.save = lambda **_k: (_ for _ in ()).throw(AssertionError("must not save"))
    assert session_squash.repair_reinjected_pre_squash_context(normal) is False
    assert len(normal.context_messages) == 1


# ── Part 4: effective-threshold resolution helper ──────────────────────────


def test_effective_threshold_floor_applies_below_512k():
    from api import compression_threshold

    info = compression_threshold.effective_threshold_percent_info(
        model="test-model",
        context_length=272000,
        config_data={"compression": {"threshold": 0.45}},
    )
    assert info["configured_percent"] == pytest.approx(0.45)
    assert info["effective_percent"] == pytest.approx(0.75)
    assert info["floor_applied"] is True


def test_effective_threshold_keeps_config_above_floor():
    from api import compression_threshold

    info = compression_threshold.effective_threshold_percent_info(
        model="test-model",
        context_length=272000,
        config_data={"compression": {"threshold": 0.8}},
    )
    assert info["effective_percent"] == pytest.approx(0.8)
    assert info["floor_applied"] is False


def test_effective_threshold_large_window_keeps_config():
    from api import compression_threshold

    info = compression_threshold.effective_threshold_percent_info(
        model="test-model",
        context_length=900000,
        config_data={"compression": {"threshold": 0.45}},
    )
    assert info["effective_percent"] == pytest.approx(0.45)
    assert info["floor_applied"] is False


def test_effective_threshold_model_override_stacks_below_floor():
    from api import compression_threshold

    info = compression_threshold.effective_threshold_percent_info(
        model="special-model",
        context_length=272000,
        config_data={
            "compression": {
                "threshold": 0.45,
                "model_thresholds": {"special-model": 0.6},
            }
        },
    )
    assert info["configured_percent"] == pytest.approx(0.6)
    assert info["effective_percent"] == pytest.approx(0.75)
    assert info["floor_applied"] is True


def test_effective_threshold_defaults_without_config():
    from api import compression_threshold

    info = compression_threshold.effective_threshold_percent_info(
        model="test-model", context_length=272000, config_data={}
    )
    assert info["configured_percent"] == pytest.approx(0.50)
    assert info["effective_percent"] == pytest.approx(0.75)
    assert info["floor_applied"] is True


def test_threshold_percent_fields_for_compressor():
    from api import compression_threshold

    compressor = _fake_compressor()
    fields = compression_threshold.threshold_percent_fields_for_compressor(compressor)
    assert fields == {
        "threshold_percent_configured": pytest.approx(0.45),
        "threshold_percent_effective": pytest.approx(0.75),
        "threshold_floor_applied": True,
    }
    assert compression_threshold.threshold_percent_fields_for_compressor(None) == {}


# ── Part 5: UI/i18n wiring ─────────────────────────────────────────────────


def test_ui_threshold_line_explains_floor():
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    assert "threshold_percent_configured" in ui_js
    assert "threshold_floor_applied" in ui_js
    assert "ctx_threshold_floor_note" in ui_js


def test_usage_merge_keeps_threshold_percent_fields():
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    merge_fn = ui_js.split("function _mergeUsageForCtxIndicator", 1)[1][:2000]
    for field in ("threshold_percent_configured", "threshold_percent_effective", "threshold_floor_applied"):
        assert field in merge_fn, f"{field} must survive the usage merge"


def test_floor_note_i18n_key_present_in_all_locales():
    i18n_js = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    # 14 locale blocks; every locale must define the floor note so the
    # explanation never falls back to a raw key.
    assert i18n_js.count("ctx_threshold_floor_note:") >= 14, (
        f"ctx_threshold_floor_note must be defined in all LOCALES blocks "
        f"(found {i18n_js.count('ctx_threshold_floor_note:')})"
    )
