"""Plan Option A, C1: mid-turn DOM collapse on the 'tail_reduced' SSE event.

The server (api/streaming.py) tells the browser to prune the live DOM above
the compression anchor mid-turn, without waiting for the terminal 'done'
event to replace the transcript wholesale. This module tests the pure-logic
slice that resolves WHERE to cut: _tailReductionCutRawIdx() locates the raw
index of the anchor message inside S.messages using the same anchor-key
shape the server emits (_compression_anchor_message_key in streaming.py:
{role, ts, text, attachments}).

Extracted + Node-vm-executed like the other ``static/ui.js`` function tests
in this suite (see test_issue2028_compression_anchor_helpers.py) so real
source-order/scope bugs are caught by CI (pytest only -- there are no
standalone tests/*.js files in this repo's CI).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"function {name} not found in ui.js"
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


ANCHOR_KEY_FN = _extract_function(UI_JS, "_compressionMessageAnchorKey")
CUT_IDX_FN = _extract_function(UI_JS, "_tailReductionCutRawIdx")
PREFIX_UI_FN = _extract_function(MESSAGES_JS, "_tailReductionPrefixUiNode")
PRUNE_PREFIX_FN = _extract_function(MESSAGES_JS, "_pruneTailReductionPrefix")


def _run_node(messages, anchor_key_input, use_helper_for_key=False):
    """Run _tailReductionCutRawIdx (and optionally _compressionMessageAnchorKey)
    in a real Node process against the exact functions extracted from the
    shipped ui.js source, and return the parsed JSON result."""
    script = f"""
{ANCHOR_KEY_FN}
{CUT_IDX_FN}
const messages = {json.dumps(messages)};
const anchorKeyInput = {json.dumps(anchor_key_input)};
const anchorKey = {"_compressionMessageAnchorKey(anchorKeyInput)" if use_helper_for_key else "anchorKeyInput"};
const cut = _tailReductionCutRawIdx(messages, anchorKey);
console.log(JSON.stringify({{cut}}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_locates_the_raw_index_of_the_anchor_message():
    messages = [
        {"role": "user", "content": "archived turn 1", "_ts": 1},
        {"role": "assistant", "content": "archived reply 1", "_ts": 2},
        {"role": "user", "content": "current question", "_ts": 3},
        {"role": "assistant", "content": "current answer", "_ts": 4},
    ]
    result = _run_node(messages, messages[2], use_helper_for_key=True)
    assert result["cut"] == 2, "cut index must point at the anchor message itself"


def test_returns_minus_one_when_anchor_not_found_fail_safe():
    messages = [{"role": "user", "content": "only message", "_ts": 1}]
    anchor_key = {"role": "assistant", "ts": 999, "text": "missing", "attachments": 0}
    result = _run_node(messages, anchor_key, use_helper_for_key=False)
    assert result["cut"] == -1, "an unresolved anchor must never guess a cut point"


def test_returns_minus_one_for_null_or_empty_anchor_key():
    messages = [{"role": "user", "content": "x", "_ts": 1}]
    result_null = _run_node(messages, None, use_helper_for_key=False)
    assert result_null["cut"] == -1

    anchor_key = {"role": "user", "ts": 1, "text": "x", "attachments": 0}
    result_empty = _run_node([], anchor_key, use_helper_for_key=False)
    assert result_empty["cut"] == -1


def test_runtime_dom_prune_preserves_virtual_geometry_controls_and_viewport():
    """Execute the real DOM-prune helpers against a virtualized prefix."""
    script = f"""
{PREFIX_UI_FN}
{PRUNE_PREFIX_FN}
class Classes {{
  constructor(...names) {{ this.names=new Set(names); }}
  contains(name) {{ return this.names.has(name); }}
}}
class Node {{
  constructor(id,height,...classes) {{
    this.id=id; this.height=height; this.classList=new Classes(...classes);
    this.parentElement=null; this.previousElementSibling=null;
  }}
  remove() {{
    const siblings=this.parentElement.children;
    siblings.splice(siblings.indexOf(this),1);
    this.parentElement.relink();
  }}
}}
class Container {{
  constructor(children) {{ this.children=children; this.relink(); }}
  relink() {{
    this.children.forEach((node,index)=>{{
      node.parentElement=this;
      node.previousElementSibling=index?this.children[index-1]:null;
    }});
  }}
}}
const spacer=new Node('top-spacer',500,'message-virtual-spacer');
const loadOlder=new Node('loadOlderIndicator',40,'load-older-indicator');
const contextBanner=new Node('context-banner',50,'ctx-brief-banner');
const archived=new Node('archived-row',120,'message-row');
const anchor=new Node('active-user-row',160,'message-row');
const container=new Container([spacer,loadOlder,contextBanner,archived,anchor]);
const scroller={{scrollTop:700,get scrollHeight(){{
  return container.children.reduce((total,node)=>total+node.height,0);
}}}};
const removed=_pruneTailReductionPrefix(container,anchor,scroller);
console.log(JSON.stringify({{
  removed,
  ids:container.children.map(node=>node.id),
  scrollTop:scroller.scrollTop,
  scrollHeight:scroller.scrollHeight,
}}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    result = json.loads(proc.stdout.strip())

    assert result == {
        "removed": 1,
        "ids": ["top-spacer", "loadOlderIndicator", "context-banner", "active-user-row"],
        "scrollTop": 580,
        "scrollHeight": 750,
    }


def test_french_tail_banner_keeps_the_session_start_label():
    """Adding compaction copy must not replace the existing French jump key."""
    french = I18N_JS.split("\n  fr: {", 1)[1].split("\n  },\n\n", 1)[0]
    assert "tail_reduced_banner:" in french
    assert "session_jump_start: 'Début'," in french
