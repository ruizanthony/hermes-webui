"""Regression coverage for internal process-wakeup transcript rendering.

A background-process wakeup stays in the persisted/model transcript as a synthetic
user turn (``_source: process_wakeup``), but it is orchestration input for the
agent, not a message written by the user. The UI must therefore hide it while
keeping the two assistant turns visually separated.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
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
function msgContent(m){
  if(!m) return '';
  if(typeof m.content === 'string') return m.content;
  if(Array.isArray(m.content)) return m.content.map(p => (p && (p.text || p.content)) || '').join('\n');
  return String(m.content || '');
}
function _isContextCompactionMessage(){ return false; }
function _isPreservedCompressionTaskListMessage(){ return false; }
function _isRecoveryControlMessage(){ return false; }
function _messageHasReasoningPayload(){ return false; }
function _assistantMessageHasVisibleContent(m){ return !!String(msgContent(m)).trim(); }

global.S = {messages: []};
let _visWithIdxCache = null;
let _visWithIdxCacheLen = 0;
let _visWithIdxCacheSrc = null;

eval(extractFunc('_messageIsRenderable'));
eval(extractFunc('_getVisibleMessagesWithIdx'));

const wakeup = {
  role: 'user',
  content: '[IMPORTANT: Background process proc_123 completed (exit_code=0).\nCommand: sleep 1\nOutput:\ndone]',
  _source: 'process_wakeup',
};
const attachmentOnlyWakeup = {
  role: 'user',
  content: '',
  _source: 'process_wakeup',
  attachments: [{name: 'result.txt'}],
};
S.messages = [
  {role: 'assistant', content: 'previous assistant report'},
  wakeup,
  {role: 'assistant', content: 'executive conclusion after wakeup'},
];
const visible = _getVisibleMessagesWithIdx();
process.stdout.write(JSON.stringify({
  visible: visible.map(e => ({rawIdx: e.rawIdx, role: e.m.role, text: String(e.m.content)})),
  wakeupRenderable: _messageIsRenderable(wakeup),
  attachmentOnlyRenderable: _messageIsRenderable(attachmentOnlyWakeup),
  persistedSource: S.messages[1]._source,
}));
"""


def _run_driver():
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_process_wakeup_is_internal_and_hidden_from_transcript():
    result = _run_driver()

    assert result["wakeupRenderable"] is False
    assert result["attachmentOnlyRenderable"] is False
    assert result["visible"] == [
        {"rawIdx": 0, "role": "assistant", "text": "previous assistant report"},
        {"rawIdx": 2, "role": "assistant", "text": "executive conclusion after wakeup"},
    ]
    assert result["persistedSource"] == "process_wakeup"


def test_hidden_process_wakeup_still_breaks_visual_assistant_turns():
    ui = UI_JS_PATH.read_text(encoding="utf-8")

    assert "if(m._source === 'process_wakeup') return false;" in ui
    assert "_hasHiddenProcessWakeupBoundaryBefore(rawIdx)" in ui
    assert "if(_hasHiddenProcessWakeupBoundaryBefore(rawIdx)) currentAssistantTurn=null;" in ui
