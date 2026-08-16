/**
 * Per-tab storage hygiene: leftover `<base>::<tabId>` keys from closed tabs
 * must be reclaimed, otherwise every new tab grows the origin's localStorage
 * footprint until it hits quota (the failure mode behind #2386).
 *
 * Run: node tests/test_tab_key_garbage_collection.js
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

function extractConst(src, name) {
  const m = src.match(new RegExp(`^const ${name}\\s*=.*?;`, 'm'));
  if (!m) throw new Error(`const ${name} not found`);
  return m[0];
}

function makeStorage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: (k) => { map.delete(String(k)); },
    key: (i) => Array.from(map.keys())[i] ?? null,
    get length() { return map.size; },
    _keys: () => Array.from(map.keys()),
  };
}

function buildContext(localStorage) {
  const sandbox = {
    localStorage,
    sessionStorage: makeStorage(),
    console, Date, JSON, Math, Object, Number, Array, String,
    setInterval: () => 0, clearInterval: () => {},
  };
  sandbox.window = sandbox;
  sandbox.window.crypto = { randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2, 12) };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);

  // Load in SOURCE ORDER so the temporal-dead-zone behaviour of the real file
  // is reproduced faithfully: if _gcOrphanTabKeys references a `const` that is
  // declared later in ui.js, this test must catch it.
  const src = [
    extractConst(UI_JS, 'TAB_ID_KEY'),
    extractConst(UI_JS, 'TAB_ID_CLAIM_KEY'),
    extractConst(UI_JS, '_TAB_CLAIM_TTL_MS'),
    extractConst(UI_JS, '_TAB_CLAIM_HEARTBEAT_MS'),
    extractConst(UI_JS, '_TAB_SEEN_TTL_MS'),
    extractConst(UI_JS, 'TAB_ID_SEEN_KEY'),
    extractConst(UI_JS, 'INFLIGHT_KEY_BASE'),
    extractConst(UI_JS, 'INFLIGHT_STATE_KEY_BASE'),
    extractConst(UI_JS, 'ACTIVE_SESSION_KEY_LEGACY'),
    extractFunction(UI_JS, '_newTabId'),
    extractFunction(UI_JS, '_readTabClaims'),
    extractFunction(UI_JS, '_writeTabClaims'),
    extractFunction(UI_JS, '_readTabSeen'),
    extractFunction(UI_JS, '_touchTabSeen'),
    extractFunction(UI_JS, '_gcOrphanTabKeys'),
    extractFunction(UI_JS, '_claimTabId'),
    extractFunction(UI_JS, '_releaseTabId'),
    extractFunction(UI_JS, '_hermesTabId'),
  ].join('\n');
  vm.runInContext(src, sandbox);
  return sandbox;
}

const results = [];
function test(name, fn) {
  try { fn(); results.push(true); console.log(`  PASS  ${name}`); }
  catch (e) { results.push(false); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}

console.log('per-tab key garbage collection');

test('orphan keys from closed tabs are reclaimed', () => {
  const store = makeStorage({
    'hermes-webui-inflight::ghost-tab-1': '{"sid":"x"}',
    'hermes-webui-inflight-state::ghost-tab-2': '{}',
    'hermes-webui-session::ghost-tab-3': 'sess-old',
    'hermes-webui-session': 'legacy-value',
    'unrelated-key': 'keep me',
  });
  const ctx = buildContext(store);
  ctx.window._hermesTabId();   // triggers _touchTabSeen + _gcOrphanTabKeys

  const keys = store._keys();
  assert.ok(!keys.includes('hermes-webui-inflight::ghost-tab-1'), 'ghost inflight key must be removed');
  assert.ok(!keys.includes('hermes-webui-inflight-state::ghost-tab-2'), 'ghost state key must be removed');
  assert.ok(!keys.includes('hermes-webui-session::ghost-tab-3'), 'ghost session key must be removed');
  assert.ok(keys.includes('hermes-webui-session'), 'legacy global key must be preserved');
  assert.ok(keys.includes('unrelated-key'), 'unrelated keys must be untouched');
});

test('a live tab keeps its own keys', () => {
  const store = makeStorage();
  const ctx = buildContext(store);
  const tabId = ctx.window._hermesTabId();
  store.setItem('hermes-webui-inflight::' + tabId, '{"sid":"mine"}');

  ctx.window._gcOrphanTabKeys();

  assert.strictEqual(store.getItem('hermes-webui-inflight::' + tabId), '{"sid":"mine"}',
    'the running tab must not delete its own state');
});

test('gc actually executes (no silently swallowed ReferenceError)', () => {
  // _gcOrphanTabKeys wraps its body in try/catch, so a temporal-dead-zone
  // ReferenceError on a later-declared const would make it a silent no-op.
  // Proving a real deletion happens is the only way to catch that.
  const store = makeStorage({'hermes-webui-inflight::ghost': 'x'});
  const ctx = buildContext(store);
  ctx.window._touchTabSeen('some-live-tab');
  ctx.window._gcOrphanTabKeys();
  assert.strictEqual(store.getItem('hermes-webui-inflight::ghost'), null,
    'gc must really delete orphan keys, not fail silently');
});

const failed = results.filter(x => !x).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
if (failed) process.exit(1);
