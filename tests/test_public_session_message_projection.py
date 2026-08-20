"""Regression coverage for the browser-facing session message projection."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_CONTENT_JSON_PREFIX = "\x00json:"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


def _disable_api_redaction(monkeypatch) -> None:
    import api.config as config

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})


def test_public_projection_decodes_hermes_structured_content_without_mutating_source(monkeypatch):
    from api.helpers import public_session_projection

    _disable_api_redaction(monkeypatch)
    parts = [
        {"type": "text", "text": "inspect this image"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    durable_content = _CONTENT_JSON_PREFIX + json.dumps(parts)
    source = {
        "session_id": "structured-session",
        "messages": [
            {
                "role": "user",
                "content": durable_content,
                "timestamp": 1000.0,
            }
        ],
    }

    projected = public_session_projection(source)

    assert projected["messages"][0]["content"] == parts
    assert source["messages"][0]["content"] == durable_content


def test_public_projection_marks_process_wakeups_as_non_human_events(monkeypatch):
    from api.helpers import public_session_projection

    _disable_api_redaction(monkeypatch)
    human = {"role": "user", "content": "real request"}
    wakeup = {
        "role": "user",
        "content": "[IMPORTANT: Background process completed]",
        "_source": "process_wakeup",
    }
    source = {
        "messages": [human, wakeup],
        "context_messages": [wakeup],
    }

    projected = public_session_projection(source)

    assert projected["messages"][0]["role"] == "user"
    assert projected["messages"][1]["role"] == "event"
    assert projected["messages"][1]["_source"] == "process_wakeup"
    assert projected["context_messages"][0]["role"] == "event"
    assert source["messages"][1]["role"] == "user"


def test_public_projection_adds_event_role_to_roleless_process_wakeup(monkeypatch):
    from api.helpers import public_session_projection

    _disable_api_redaction(monkeypatch)
    projected = public_session_projection(
        {"messages": [{"content": "background completed", "_source": "process_wakeup"}]}
    )

    assert projected["messages"][0]["role"] == "event"
    assert projected["messages"][0]["_source"] == "process_wakeup"


def test_public_projection_preserves_malformed_structured_content(monkeypatch):
    from api.helpers import public_session_projection

    _disable_api_redaction(monkeypatch)
    malformed = _CONTENT_JSON_PREFIX + "{not-json"

    projected = public_session_projection(
        {"messages": [{"role": "user", "content": malformed}]}
    )

    assert projected["messages"][0]["content"] == malformed


def test_public_projection_fails_closed_on_over_nested_structured_content(monkeypatch):
    from api.helpers import public_session_projection

    _disable_api_redaction(monkeypatch)
    malformed = _CONTENT_JSON_PREFIX + ("[" * 1500) + "0" + ("]" * 1500)

    projected = public_session_projection(
        {"messages": [{"role": "user", "content": malformed}]}
    )

    assert projected["messages"][0]["content"] == malformed


def test_decoded_structured_content_scrubs_internal_part_aliases(monkeypatch):
    from api.helpers import public_session_projection

    _disable_api_redaction(monkeypatch)
    parts = [
        {
            "type": "text",
            "text": "visible",
            "api_content": "internal sidecar",
            "_db_row_id": 41,
        }
    ]
    durable_content = _CONTENT_JSON_PREFIX + json.dumps(parts)
    source = {"messages": [{"role": "user", "content": durable_content}]}

    projected = public_session_projection(source)

    assert projected["messages"][0]["content"] == [
        {"type": "text", "text": "visible"}
    ]
    assert source["messages"][0]["content"] == durable_content


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_public_event_role_preserves_silent_wakeup_turn_collapse():
    assert _NODE is not None
    driver = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunc(name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){
      depth--;
      if(depth === 0){ close = i; break; }
    }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}
function msgContent(m){ return String((m && m.content) || ''); }
function _isSilentSentinelContent(text){ return String(text || '').trim() === '[[SILENT]]'; }
eval(extractFunc('_computeSilentTurnHiddenIdxs'));
const messages = [
  {role: 'user', content: 'question humaine'},
  {role: 'assistant', content: 'réponse visible'},
  {role: 'event', content: '[background complete]', _source: 'process_wakeup'},
  {role: 'assistant', content: '[[SILENT]]'},
];
process.stdout.write(JSON.stringify(Array.from(_computeSilentTurnHiddenIdxs(messages)).sort()));
"""

    result = subprocess.run(
        [_NODE, "-e", driver, str(_ROOT / "static" / "ui.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == [2, 3]


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_regenerate_does_not_cross_process_wakeup_boundary():
    assert _NODE is not None
    driver = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunc(name){
  const asyncStart = src.indexOf('async function ' + name);
  const plainStart = src.indexOf('function ' + name);
  const start = asyncStart >= 0 ? asyncStart : plainStart;
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){
      depth--;
      if(depth === 0){ close = i; break; }
    }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}
const input = {value: ''};
const apiCalls = [];
const sends = [];
let _oldestIdx = 0;
let S = {
  session: {session_id: 's1'},
  busy: false,
  messages: [
    {role: 'user', content: 'ancienne demande humaine'},
    {role: 'assistant', content: 'ancienne réponse'},
    {role: 'event', content: 'réveil synthétique', _source: 'process_wakeup'},
    {role: 'assistant', content: 'résultat du réveil'},
  ],
};
function msgContent(m){ return String((m && m.content) || ''); }
function $(id){ return id === 'msg' ? input : null; }
function renderMessages(){}
function setStatus(){}
function t(key){ return key; }
async function _ensureAllMessagesLoaded(){}
async function api(path, options){ apiCalls.push({path, options}); return {}; }
async function send(){ sends.push(input.value); }
eval(extractFunc('regenerateResponse'));
const button = {closest: () => ({dataset: {msgIdx: '3'}})};
(async () => {
  await regenerateResponse(button);
  process.stdout.write(JSON.stringify({apiCalls, sends, input: input.value}));
})().catch((error) => { console.error(error); process.exit(1); });
"""

    result = subprocess.run(
        [_NODE, "-e", driver, str(_ROOT / "static" / "ui.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {"apiCalls": [], "sends": [], "input": ""}
