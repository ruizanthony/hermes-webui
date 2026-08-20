"""Archiving a collapsed compression lineage must archive the whole lineage.

Regression coverage for the "Archive does nothing" sidebar bug.

The sidebar collapses a compression lineage (a chain of ``compression``
continuation segments) into a SINGLE representative row. The archive action,
however, only ever flipped ``archived`` on the one ``session_id`` carried by
that row. The next segment of the same lineage was immediately promoted as the
new representative, so the row reappeared and the user saw "I click archive and
nothing happens".

Reproduced on the live service before the fix:

    lineage of 39 segments, representative 20260813_233334_eeabc5
    POST /api/session/archive {session_id: 20260813_233334_eeabc5} -> 200 OK
    sidebar re-render -> row still present, now 20260813_232349_7b71e4

The decision (which row to show) and the action (which row to archive) must
operate on the same resolved unit: the lineage, not one segment of it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_agent_sessions():
    if "api.agent_sessions" in sys.modules:
        return sys.modules["api.agent_sessions"]
    spec = importlib.util.spec_from_file_location(
        "api.agent_sessions", REPO_ROOT / "api" / "agent_sessions.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("api.agent_sessions", module)
    spec.loader.exec_module(module)
    return module


agent_sessions = _load_agent_sessions()


def _rows_for_lineage():
    """Three compression segments forming one collapsed sidebar lineage."""
    return {
        "root": {
            "id": "root",
            "parent_session_id": None,
            "source": "webui",
            "end_reason": "compression",
        },
        "mid": {
            "id": "mid",
            "parent_session_id": "root",
            "source": "webui",
            "end_reason": "compression",
        },
        "tip": {
            "id": "tip",
            "parent_session_id": "mid",
            "source": "webui",
            "end_reason": "compression",
        },
    }


def test_lineage_member_ids_covers_every_compression_segment():
    """The archive fan-out set must contain all segments, not just the clicked one.

    Without this, archiving the representative leaves the siblings unarchived
    and one of them takes its place in the sidebar on the next render.
    """
    resolver = getattr(agent_sessions, "compression_lineage_member_ids", None)
    assert resolver is not None, (
        "api.agent_sessions must expose compression_lineage_member_ids() so the "
        "archive route can fan out across a collapsed lineage"
    )

    rows = _rows_for_lineage()
    for clicked in ("root", "mid", "tip"):
        members = resolver(rows, clicked)
        assert members == {"root", "mid", "tip"}, (
            f"archiving {clicked} must resolve the full lineage; got {members}"
        )


def test_lineage_member_ids_stops_at_non_continuation_boundaries():
    """A delegated subagent child is NOT a compression segment of its parent.

    Fanning out across it would archive an unrelated conversation, so the walk
    must stop at any edge that is not a compression continuation.
    """
    resolver = getattr(agent_sessions, "compression_lineage_member_ids", None)
    assert resolver is not None

    rows = _rows_for_lineage()
    rows["delegated"] = {
        "id": "delegated",
        "parent_session_id": "mid",
        "source": "subagent",
        "end_reason": "agent_close",
    }
    members = resolver(rows, "mid")
    assert "delegated" not in members, (
        "a delegated subagent child must never be swept into its parent's "
        f"lineage archive; got {members}"
    )
    assert members == {"root", "mid", "tip"}


def test_lineage_member_ids_is_cycle_safe_and_total():
    """Hostile/corrupt parent links must not hang or drop the clicked session."""
    resolver = getattr(agent_sessions, "compression_lineage_member_ids", None)
    assert resolver is not None

    cyclic = {
        "a": {
            "id": "a",
            "parent_session_id": "b",
            "source": "webui",
            "end_reason": "compression",
        },
        "b": {
            "id": "b",
            "parent_session_id": "a",
            "source": "webui",
            "end_reason": "compression",
        },
    }
    assert resolver(cyclic, "a") == {"a", "b"}

    # Unknown session: never return an empty set for the clicked id, otherwise
    # the archive route would silently no-op.
    assert resolver(_rows_for_lineage(), "ghost") == {"ghost"}
    assert resolver({}, "") == set()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
