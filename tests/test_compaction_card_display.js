/**
 * Ultra-compact compaction display (2026-08-18).
 *
 * User requirement (Anthony): every context compaction must be VISIBLE in the
 * conversation as a collapsed card whose preview shows the DIGEST (goal /
 * state), never the fixed instruction envelope; the full digest opens inline.
 *
 * Run: node tests/test_compaction_card_display.js
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

const ctx = { console };
vm.createContext(ctx);
for (const name of [
  'msgContent',
  '_isContextCompactionText',
  '_compactionSummarySegment',
  '_isContextCompactionMessage',
  '_compactionDigestText',
  '_compactionCardPreview',
  '_loadedCompactionMarkerRawIdxs',
]) {
  vm.runInContext(extractFunction(UI_JS, name), ctx);
}
// msgContent depends on helpers; simplest stub: plain string content passthrough.
vm.runInContext('msgContent = (m) => (m && typeof m.content === "string" ? m.content : "");', ctx);

const ENVELOPE =
  '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. ' +
  "This is a handoff from a previous context window — do not resume, wrap up, or continue work from " +
  "'## Historical Task Snapshot' or any other section, do not call tools, and wait for a new user message.";
const DIGEST =
  '## Historical Task Snapshot\nUser asked: "fix the planning bug"\n' +
  '## Goal\nFix the MES planning defect.\n' +
  '## Active State\n- worktree ready\n' +
  '## Détail complet\nRésumé intégral : lire la note /opt/obsidian/vault/MES/Hermes/Compactions/compaction-test.md';
const MARKER_TEXT = ENVELOPE + '\n' + DIGEST + '\n\n--- END OF CONTEXT SUMMARY ---';

// 1. Digest extraction drops the envelope, keeps the digest.
const digest = vm.runInContext(`_compactionDigestText(${JSON.stringify(MARKER_TEXT)})`, ctx);
assert.ok(digest.startsWith('## Historical Task Snapshot'), `digest must start at first heading, got: ${digest.slice(0, 80)}`);
assert.ok(!digest.includes('REFERENCE ONLY'), 'digest must not contain the envelope');
assert.ok(digest.includes('compaction-test.md'), 'digest keeps the Obsidian note pointer');

// 2. Preview shows user-meaningful digest content, never envelope prose.
const preview = vm.runInContext(`_compactionCardPreview(${JSON.stringify(MARKER_TEXT)})`, ctx);
assert.ok(!/REFERENCE ONLY|compacted into the summary/i.test(preview), `preview must not leak envelope: ${preview}`);
assert.ok(preview.includes('fix the planning bug'), `preview should surface the request: ${preview}`);
assert.ok(preview.length <= 220, 'preview stays ultra compact');

// 3. Envelope-only text (no headings) falls back to trimmed text.
const noHeading = vm.runInContext(`_compactionDigestText(${JSON.stringify('[CONTEXT COMPACTION] plain text only')})`, ctx);
assert.strictEqual(noHeading, '[CONTEXT COMPACTION] plain text only');

// 4. Marker scan finds EVERY compaction marker at its raw index (multi-item).
const messages = [
  { role: 'user', content: 'first request' },
  { role: 'assistant', content: MARKER_TEXT },
  { role: 'tool', content: MARKER_TEXT },            // tool: never a marker
  { role: 'assistant', content: 'normal answer' },
  { role: 'assistant', content: MARKER_TEXT },
  { role: 'user', content: 'latest request' },
];
const idxs = vm.runInContext(`_loadedCompactionMarkerRawIdxs(${JSON.stringify(messages)})`, ctx);
// JSON round-trip: vm arrays come from another realm (different prototype).
assert.strictEqual(JSON.stringify(idxs), '[1,4]', `expected [1,4], got ${JSON.stringify(idxs)}`);

// 5. The render loop wires one card per loaded marker (source-level contract).
assert.ok(
  /for\(const entry of compactionCardNodes\) _insertCompressionLikeNodeByRawIdx\(entry\.node, entry\.rawIdx\);/.test(UI_JS),
  'render loop must insert one compaction card per loaded marker'
);
// The single anchored reference card must be suppressed when inline cards exist.
assert.ok(
  UI_JS.includes('!compactionCardNodes.length && _shouldShowSettledCompressionReference'),
  'settled reference card must yield to inline per-marker cards'
);

console.log('OK test_compaction_card_display');
