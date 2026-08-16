/**
 * Multi-tab session isolation — behavioural regression test.
 *
 * These are EXECUTION tests, not source-grep tests: the real helper functions
 * are extracted from static/ui.js and run against a simulated pair of browser
 * tabs sharing one localStorage (exactly how Chrome behaves) with independent
 * sessionStorage (also exactly how Chrome behaves).
 *
 * Bugs covered:
 *   1. Two tabs overwrote each other's "active session" slot, so reloading
 *      tab 1 restored the conversation last opened in tab 2.
 *   2. markInflight() wrote a single global key, so tab 2 starting a turn
 *      clobbered tab 1's reconnect marker.
 *   3. saveInflightState()/loadInflightState() shared one map, so one tab's
 *      live transcript snapshot could be merged into another tab's chat.
 *
 * Run: node tests/test_multi_tab_session_isolation.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const UI_JS = fs.readFileSync(path.join(__dirname, '..', 'static', 'ui.js'), 'utf8');

/** Extract a top-level `function name(...) {...}` body including signature. */
function extractFunction(src, name) {
  const marker = `function ${name}(`;
  const start = src.indexOf(marker);
  if (start === -1) throw new Error(`function ${name} not found in ui.js`);
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

/** Extract a top-level `const NAME = ...;` single-line declaration. */
function extractConst(src, name) {
  const re = new RegExp(`^const ${name}\\s*=.*?;`, 'm');
  const m = src.match(re);
  if (!m) throw new Error(`const ${name} not found in ui.js`);
  return m[0];
}

/** Minimal Storage implementation matching the DOM spec surface we use. */
function makeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: (k) => { map.delete(String(k)); },
    clear: () => map.clear(),
    key: (i) => Array.from(map.keys())[i] ?? null,
    get length() { return map.size; },
    _dump: () => Object.fromEntries(map),
  };
}

/**
 * Build one simulated browser tab: shared localStorage, private sessionStorage,
 * real helper functions from ui.js evaluated inside it.
 */
function makeTab(sharedLocalStorage) {
  const sandbox = {
    localStorage: sharedLocalStorage,
    sessionStorage: makeStorage(),
    console,
    Date,
    JSON,
    Math,
    Object,
    Number,
    Array,
    String,
    setInterval: () => 0,      // no timers under test
    clearInterval: () => {},
  };
  sandbox.window = sandbox;
  sandbox.window.crypto = {
    randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2, 12),
  };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);

  const pieces = [
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
    extractFunction(UI_JS, '_inflightKey'),
    extractFunction(UI_JS, '_inflightStateKey'),
    extractFunction(UI_JS, '_activeSessionKey'),
    extractFunction(UI_JS, '_rememberActiveSession'),
    extractFunction(UI_JS, '_rememberedActiveSession'),
    extractFunction(UI_JS, '_forgetActiveSession'),
  ].join('\n');

  vm.runInContext(pieces, sandbox);

  // These reference INFLIGHT_STATE_KEY / limits, so define the accessor first.
  vm.runInContext(`
    Object.defineProperty(window, 'INFLIGHT_KEY', {get:_inflightKey, configurable:true});
    Object.defineProperty(window, 'INFLIGHT_STATE_KEY', {get:_inflightStateKey, configurable:true});
    const INFLIGHT_STATE_DEFAULT_LIMITS_OBJ = ${JSON.stringify({
      maxSessions: 8, messages: 24, toolCalls: 48, stringChars: 60000, jsonChars: 1500000,
    })};
    function _getInflightStateLimits(){ return INFLIGHT_STATE_DEFAULT_LIMITS_OBJ; }
    function _isStorageQuotaError(){ return false; }
    function _truncateInflightValue(v){ return v; }
    function _compactInflightState(state){
      return {streamId: state.streamId||null, messages: state.messages||[]};
    }
  `, sandbox);

  vm.runInContext([
    extractFunction(UI_JS, '_readInflightStateMap'),
    extractFunction(UI_JS, '_writeInflightStateMap'),
    extractFunction(UI_JS, 'saveInflightState'),
    extractFunction(UI_JS, 'loadInflightState'),
    extractFunction(UI_JS, 'clearInflightState'),
    extractFunction(UI_JS, 'markInflight'),
    extractFunction(UI_JS, 'clearInflight'),
  ].join('\n'), sandbox);

  return sandbox;
}

const results = [];
function test(name, fn) {
  try {
    fn();
    results.push([true, name]);
    console.log(`  PASS  ${name}`);
  } catch (err) {
    results.push([false, name]);
    console.log(`  FAIL  ${name}\n        ${err.message}`);
  }
}

console.log('multi-tab session isolation');

// ── Bug 1: active session slot ──────────────────────────────────────────────
test('two tabs keep independent active sessions', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const tabB = makeTab(shared);

  tabA.window._rememberActiveSession('session-AAA');
  tabB.window._rememberActiveSession('session-BBB');

  // Before the fix both tabs read the same global key and saw 'session-BBB'.
  assert.strictEqual(tabA.window._rememberedActiveSession(), 'session-AAA',
    'tab A must still restore its own conversation');
  assert.strictEqual(tabB.window._rememberedActiveSession(), 'session-BBB',
    'tab B must still restore its own conversation');
});

test('tab ids are distinct across concurrent tabs', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const tabB = makeTab(shared);
  assert.notStrictEqual(tabA.window._hermesTabId(), tabB.window._hermesTabId());
});

test('duplicated tab (copied sessionStorage) gets a fresh id', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const idA = tabA.window._hermesTabId();

  // Simulate Chrome "Duplicate tab": sessionStorage is COPIED.
  const clone = makeTab(shared);
  clone.sessionStorage.setItem('hermes-webui-tab-id', idA);
  const idClone = clone.window._hermesTabId();

  assert.notStrictEqual(idClone, idA,
    'a duplicated tab must not reuse the original tab id');
});

// ── Bug 2: reconnect marker ─────────────────────────────────────────────────
test('one tab starting a turn does not clobber the other tab reconnect marker', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const tabB = makeTab(shared);

  tabA.window.markInflight('session-AAA', 'stream-A');
  tabB.window.markInflight('session-BBB', 'stream-B');

  const rawA = shared.getItem(tabA.window.INFLIGHT_KEY);
  const rawB = shared.getItem(tabB.window.INFLIGHT_KEY);
  assert.ok(rawA, 'tab A marker must survive tab B starting a turn');
  assert.strictEqual(JSON.parse(rawA).sid, 'session-AAA');
  assert.strictEqual(JSON.parse(rawB).sid, 'session-BBB');
});

test('clearInflight only clears the calling tab', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const tabB = makeTab(shared);
  tabA.window.markInflight('session-AAA', 'stream-A');
  tabB.window.markInflight('session-BBB', 'stream-B');

  tabB.window.clearInflight();

  assert.ok(shared.getItem(tabA.window.INFLIGHT_KEY),
    'tab A marker must survive tab B clearing its own');
  assert.strictEqual(shared.getItem(tabB.window.INFLIGHT_KEY), null);
});

// ── Bug 3: transcript snapshots bleeding across tabs ────────────────────────
test('inflight transcript snapshots do not leak between tabs', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const tabB = makeTab(shared);

  tabA.window.saveInflightState('shared-session', {
    streamId: 'stream-A',
    messages: [{role: 'user', content: 'FROM TAB A'}],
  });
  tabB.window.saveInflightState('shared-session', {
    streamId: 'stream-B',
    messages: [{role: 'user', content: 'FROM TAB B'}],
  });

  const fromA = tabA.window.loadInflightState('shared-session', 'stream-A');
  const fromB = tabB.window.loadInflightState('shared-session', 'stream-B');

  assert.ok(fromA, 'tab A must still find its own snapshot');
  assert.strictEqual(fromA.messages[0].content, 'FROM TAB A',
    "tab A must not read tab B's transcript");
  assert.ok(fromB, 'tab B must still find its own snapshot');
  assert.strictEqual(fromB.messages[0].content, 'FROM TAB B');
});

test('a snapshot owned by another tab is rejected', () => {
  const shared = makeStorage();
  const tabA = makeTab(shared);
  const tabB = makeTab(shared);

  tabA.window.saveInflightState('sess-1', {
    streamId: 'stream-A',
    messages: [{role: 'user', content: 'FROM TAB A'}],
  });

  // Force tab B to look under tab A's storage key: ownership must still reject.
  const rawA = shared.getItem(tabA.window.INFLIGHT_STATE_KEY);
  shared.setItem(tabB.window.INFLIGHT_STATE_KEY, rawA);

  const leaked = tabB.window.loadInflightState('sess-1', null);
  assert.strictEqual(leaked, null,
    "tab B must reject a snapshot stamped with tab A's id");
});

test('same-tab reload still recovers its own snapshot', () => {
  const shared = makeStorage();
  const tab = makeTab(shared);
  const tabId = tab.window._hermesTabId();

  tab.window.saveInflightState('sess-1', {
    streamId: 'stream-1',
    messages: [{role: 'user', content: 'MID STREAM'}],
  });
  tab.window._rememberActiveSession('sess-1');

  // Reload: same sessionStorage, fresh JS context, claim released on pagehide.
  tab.window._releaseTabId();
  const reloaded = makeTab(shared);
  reloaded.sessionStorage.setItem('hermes-webui-tab-id', tabId);

  assert.strictEqual(reloaded.window._hermesTabId(), tabId,
    'a plain reload must keep the same tab id');
  const recovered = reloaded.window.loadInflightState('sess-1', 'stream-1');
  assert.ok(recovered, 'reload must recover its own in-flight snapshot');
  assert.strictEqual(recovered.messages[0].content, 'MID STREAM');
  assert.strictEqual(reloaded.window._rememberedActiveSession(), 'sess-1');
});

const failed = results.filter(([ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) process.exit(1);
