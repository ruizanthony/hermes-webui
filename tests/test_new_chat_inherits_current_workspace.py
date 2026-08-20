"""Le workspace d'une nouvelle conversation suit la conversation courante, pas un pointeur global.

Contexte (Anthony, 2026-08-20) : plusieurs conversations tournent en parallèle sur des
workspaces différents. `newSession()` résolvait le workspace ainsi :

    switchWs || S._profileDefaultWorkspace || S.session.workspace

`S._profileDefaultWorkspace` est global au profil et, pire, `get_profile_default_workspace()`
le dérive de `last_workspace.txt` (api/workspace.py ~419-427) que TOUTE conversation réécrit
via `set_last_workspace()`. Ouvrir une nouvelle conversation depuis MES atterrissait donc sur
le workspace utilisé en dernier par une autre conversation.

Ordre attendu : switchWs || S.session.workspace || S._profileDefaultWorkspace
"""

import json
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"


def _node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


node_test = pytest.mark.skipif(not _node_available(), reason="node introuvable")


def _inherit_expression():
    """Extrait l'expression de résolution du workspace depuis newSession()."""
    src = SESSIONS_JS.read_text(encoding="utf-8")
    match = re.search(r"const inheritWs=([^;]+);", src)
    assert match, "expression inheritWs introuvable dans newSession()"
    return match.group(1)


def _eval_inherit(session_workspace, profile_default, switch_ws=None):
    """Évalue réellement l'expression extraite sous Node, avec un état simulé."""
    expr = _inherit_expression()
    driver = f"""
    const S = {{
      session: {json.dumps({"workspace": session_workspace}) if session_workspace is not None else "null"},
      _profileDefaultWorkspace: {json.dumps(profile_default)},
      _profileSwitchWorkspace: {json.dumps(switch_ws)},
    }};
    const switchWs = S._profileSwitchWorkspace;
    S._profileSwitchWorkspace = null;
    const inheritWs = {expr};
    console.log(JSON.stringify({{
      inheritWs: inheritWs,
      switchConsumed: S._profileSwitchWorkspace === null,
      profileDefaultPreserved: S._profileDefaultWorkspace,
    }}));
    """
    out = subprocess.run(
        ["node", "-e", driver], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@node_test
def test_new_chat_from_a_conversation_inherits_that_conversation_workspace():
    """Le cas d'usage d'Anthony : nouvelle conversation depuis MES => MES."""
    res = _eval_inherit(
        session_workspace="/a0/usr/projects/MES",
        profile_default="/opt/hermes-webui",
    )
    assert res["inheritWs"] == "/a0/usr/projects/MES", (
        "une nouvelle conversation doit hériter du workspace de la conversation "
        f"courante, pas du pointeur global (obtenu: {res['inheritWs']})"
    )


@node_test
def test_blank_page_still_falls_back_to_profile_default():
    """Sans conversation chargée, le défaut de profil reste le repli (#804/#5169)."""
    res = _eval_inherit(session_workspace=None, profile_default="/opt/hermes-webui")
    assert res["inheritWs"] == "/opt/hermes-webui"


@node_test
def test_profile_switch_workspace_still_wins_and_is_consumed():
    """Le drapeau one-shot de bascule de profil garde la priorité absolue (#823)."""
    res = _eval_inherit(
        session_workspace="/a0/usr/projects/MES",
        profile_default="/opt/hermes-webui",
        switch_ws="/switched",
    )
    assert res["inheritWs"] == "/switched"
    assert res["switchConsumed"] is True
    assert res["profileDefaultPreserved"] == "/opt/hermes-webui"
