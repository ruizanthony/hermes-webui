"""The context brief must preserve the prompt→conclusion sequence across compaction.

User report (2026-08-17): after live compaction, the brief no longer showed the
initial user request — the child sidecar starts at the compression anchor, and
build_deterministic_brief read only the child's own messages.

Contract under test:
- _session_messages stitches pre_compression_snapshot parents (same lineage
  helper the transcript uses), so the ORIGINAL ask and PRE-compaction
  conclusions are visible to the brief;
- the request/conclusion caps keep the FIRST entry plus the recent tail;
- forks / no-parent sessions / stitch failures fall back to own messages.
"""

import importlib
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def brief_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state" / "sessions").mkdir(parents=True)
    for mod in ("api.config", "api.models", "api.routes", "api.context_brief"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("api.config")
    models = importlib.import_module("api.models")
    routes = importlib.import_module("api.routes")
    brief = importlib.import_module("api.context_brief")
    yield types.SimpleNamespace(config=config, models=models, routes=routes, brief=brief)
    for mod in ("api.context_brief", "api.routes", "api.models", "api.config"):
        sys.modules.pop(mod, None)


def _build_compacted_lineage(env):
    """Parent snapshot with the original ask; child continuation without it."""
    models = env.models
    now = time.time()
    parent = models.Session(session_id="20260101_000000_parent")
    parent.messages = [
        {"role": "user", "content": "DEMANDE INITIALE: corrige le module planning", "timestamp": now},
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 Réponse / recommandation: premier correctif livré", "timestamp": now + 10},
    ]
    parent.pre_compression_snapshot = True
    parent.save()

    child = models.Session(session_id="20260101_000100_child0")
    child.parent_session_id = parent.session_id
    child.messages = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION] resumé du travail", "timestamp": now + 20},
        {"role": "user", "content": "poursuis avec le second lot", "timestamp": now + 30},
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 Réponse / recommandation: second lot livré", "timestamp": now + 40},
    ]
    child.save()
    return parent, child


def test_brief_includes_pre_compaction_request(brief_env):
    _parent, child = _build_compacted_lineage(brief_env)
    brief = brief_env.brief.build_deterministic_brief(child, child.session_id, source="webui")
    texts = [r["text"] for r in brief["requests"]]
    assert any("DEMANDE INITIALE" in t for t in texts), texts


def test_brief_includes_pre_compaction_conclusion(brief_env):
    _parent, child = _build_compacted_lineage(brief_env)
    brief = brief_env.brief.build_deterministic_brief(child, child.session_id, source="webui")
    excerpts = [c["excerpt"] for c in brief["accomplished"]["conclusions"]]
    assert any("premier correctif" in e for e in excerpts), excerpts
    assert any("second lot" in e for e in excerpts), excerpts


def test_request_cap_keeps_first_request(brief_env):
    models = brief_env.models
    now = time.time()
    s = models.Session(session_id="20260101_000200_caps00")
    s.messages = [
        {"role": "user", "content": "PREMIERE DEMANDE fondatrice", "timestamp": now},
    ]
    for i in range(20):
        s.messages.append({
            "role": "user", "content": f"demande de suivi {i}", "timestamp": now + 10 + i,
        })
    s.save()
    brief = brief_env.brief.build_deterministic_brief(s, s.session_id, source="webui")
    texts = [r["text"] for r in brief["requests"]]
    assert any("PREMIERE DEMANDE" in t for t in texts), texts
    assert brief["request_count"] == 21


def test_no_parent_session_unchanged(brief_env):
    models = brief_env.models
    now = time.time()
    s = models.Session(session_id="20260101_000300_plain0")
    s.messages = [
        {"role": "user", "content": "simple ask", "timestamp": now},
        {"role": "assistant", "content": "simple answer", "timestamp": now + 1},
    ]
    s.save()
    msgs = brief_env.brief._session_messages(s)
    assert [m["content"] for m in msgs] == ["simple ask", "simple answer"]


def test_fork_parent_not_stitched(brief_env):
    """Ordinary forks share parent_session_id but must stay independent."""
    models = brief_env.models
    now = time.time()
    parent = models.Session(session_id="20260101_000400_forkpa")
    parent.messages = [
        {"role": "user", "content": "parent-only content", "timestamp": now},
    ]
    parent.save()  # NOT pre_compression_snapshot
    child = models.Session(session_id="20260101_000401_forkch")
    child.parent_session_id = parent.session_id
    child.messages = [
        {"role": "user", "content": "fork child ask", "timestamp": now + 5},
    ]
    child.save()
    msgs = brief_env.brief._session_messages(child)
    assert [m["content"] for m in msgs] == ["fork child ask"]
