/**
 * Plan Option A, C1: the 'tail_reduced' SSE event must let the browser prune
 * the live DOM above the compression anchor mid-turn, without waiting for the
 * terminal 'done' event to replace the transcript wholesale (#webui-compaction
 * visibility fix, 2026-08-16).
 *
 * Pure-logic slice only (no DOM): _tailReductionCutRawIdx() locates the raw
 * index of the anchor message inside S.messages using the same anchor-key
 * shape the server emits (_compression_anchor_message_key in streaming.py:
 * {role, ts, text, attachments}). Extracted + vm-executed like
 * test_tab_key_garbage_collection.js so real source-order bugs are caught.
 *
 * Run: node tests/test_compressed_dom_collapse.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const UI_JS = fs.readFileSync(path.join(__dirname, '..', 'static', 'ui.js'), 'utf8');

function extractFunction(src, name) {
  const marker = `function ${name}(`;
  const start = src.indexOf(marker);
  if (start === -1) throw new Error(`function ${name} not found`);
  const brace = src.indexOf('{', start);
  let depth = 1;
  let i = brace + 1;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

function buildContext() {
  const sandbox = { console, JSON, Math, Object, Number, Array, String };
  vm.createContext(sandbox);
  const src = [
    extractFunction(UI_JS, '_compressionMessageAnchorKey'),
    extractFunction(UI_JS, '_tailReductionCutRawIdx'),
  ].join('\n');
  vm.runInContext(src, sandbox);
  return sandbox;
}

const results = [];
function test(name, fn) {
  try { fn(); results.push(true); console.log(`  PASS  ${name}`); }
  catch (e) { results.push(false); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}

console.log('compressed-event mid-turn DOM collapse (tail_reduced)');

test('locates the raw index of the anchor message', () => {
  const ctx = buildContext();
  const messages = [
    { role: 'user', content: 'archived turn 1', _ts: 1 },
    { role: 'assistant', content: 'archived reply 1', _ts: 2 },
    { role: 'user', content: 'current question', _ts: 3 },
    { role: 'assistant', content: 'current answer', _ts: 4 },
  ];
  const anchorKey = ctx.window
    ? ctx.window._compressionMessageAnchorKey(messages[2])
    : ctx._compressionMessageAnchorKey(messages[2]);
  const cut = ctx._tailReductionCutRawIdx(messages, anchorKey);
  assert.strictEqual(cut, 2, 'cut index must point at the anchor message itself');
});

test('returns -1 when the anchor is not found (fail-safe no-op)', () => {
  const ctx = buildContext();
  const messages = [
    { role: 'user', content: 'only message', _ts: 1 },
  ];
  const cut = ctx._tailReductionCutRawIdx(messages, { role: 'assistant', ts: 999, text: 'missing', attachments: 0 });
  assert.strictEqual(cut, -1, 'an unresolved anchor must never guess a cut point');
});

test('returns -1 for an empty or null anchor key (never drops everything)', () => {
  const ctx = buildContext();
  const messages = [{ role: 'user', content: 'x', _ts: 1 }];
  assert.strictEqual(ctx._tailReductionCutRawIdx(messages, null), -1);
  assert.strictEqual(ctx._tailReductionCutRawIdx([], { role: 'user', ts: 1, text: 'x', attachments: 0 }), -1);
});

const failed = results.filter(x => !x).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
