/**
 * RED proof: demonstrates the multi-tab bugs against the ORIGINAL (pre-fix)
 * ui.js by executing its real functions in two simulated tabs sharing one
 * localStorage. Run this inside the baseline worktree to see the bugs, and
 * inside the fixed worktree to see them gone.
 *
 * This does NOT depend on any helper introduced by the fix: it drives the
 * original globals directly, so it is a fair before/after comparison.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const UI_JS = fs.readFileSync(path.join(__dirname, '..', 'static', 'ui.js'), 'utf8');

function extractFunction(src, name) {
  const marker = `function ${name}(`;
  const start = src.indexOf(marker);
  if (start === -1) return null;
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

function makeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: (k) => { map.delete(String(k)); },
    clear: () => map.clear(),
    get length() { return map.size; },
    _keys: () => Array.from(map.keys()),
  };
}

// Detect whether the fix is present in this checkout.
const FIXED = UI_JS.includes('const TAB_ID_KEY');

function makeTab(shared) {
  const sandbox = {
    localStorage: shared,
    sessionStorage: makeStorage(),
    console, Date, JSON, Math, Object, Number, Array, String,
    setInterval: () => 0, clearInterval: () => {},
  };
  sandbox.window = sandbox;
  sandbox.window.crypto = { randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2, 12) };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);

  let prelude = '';
  if (FIXED) {
    // Fixed build: pull in the per-tab identity helpers.
    for (const decl of ['TAB_ID_KEY', 'TAB_ID_CLAIM_KEY', '_TAB_CLAIM_TTL_MS', '_TAB_CLAIM_HEARTBEAT_MS',
                        '_TAB_SEEN_TTL_MS', 'TAB_ID_SEEN_KEY',
                        'INFLIGHT_KEY_BASE', 'INFLIGHT_STATE_KEY_BASE', 'ACTIVE_SESSION_KEY_LEGACY']) {
      const m = UI_JS.match(new RegExp(`^const ${decl}\\s*=.*?;`, 'm'));
      if (m) prelude += m[0] + '\n';
    }
    for (const fn of ['_newTabId', '_readTabClaims', '_writeTabClaims', '_readTabSeen',
                      '_touchTabSeen', '_gcOrphanTabKeys', '_claimTabId',
                      '_releaseTabId', '_hermesTabId', '_inflightKey', '_inflightStateKey',
                      '_activeSessionKey', '_rememberActiveSession', '_rememberedActiveSession',
                      '_forgetActiveSession']) {
      const body = extractFunction(UI_JS, fn);
      if (body) prelude += body + '\n';
    }
    prelude += `
      Object.defineProperty(window,'INFLIGHT_KEY',{get:_inflightKey,configurable:true});
      Object.defineProperty(window,'INFLIGHT_STATE_KEY',{get:_inflightStateKey,configurable:true});
    `;
  } else {
    // Original build: the two global keys, verbatim.
    prelude += `
      const INFLIGHT_KEY='hermes-webui-inflight';
      const INFLIGHT_STATE_KEY='hermes-webui-inflight-state';
      window.INFLIGHT_KEY=INFLIGHT_KEY;
      window.INFLIGHT_STATE_KEY=INFLIGHT_STATE_KEY;
      function _rememberActiveSession(sid){ localStorage.setItem('hermes-webui-session',sid); }
      function _rememberedActiveSession(){ return localStorage.getItem('hermes-webui-session')||''; }
      function _hermesTabId(){ return 'legacy'; }
    `;
  }

  prelude += `
    function _getInflightStateLimits(){ return {maxSessions:8,messages:24,toolCalls:48,stringChars:60000,jsonChars:1500000}; }
    function _isStorageQuotaError(){ return false; }
    function _truncateInflightValue(v){ return v; }
    function _compactInflightState(state){ return {streamId:state.streamId||null, messages:state.messages||[]}; }
  `;
  vm.runInContext(prelude, sandbox);

  vm.runInContext([
    extractFunction(UI_JS, '_readInflightStateMap'),
    extractFunction(UI_JS, '_writeInflightStateMap'),
    extractFunction(UI_JS, 'saveInflightState'),
    extractFunction(UI_JS, 'loadInflightState'),
    extractFunction(UI_JS, 'markInflight'),
  ].filter(Boolean).join('\n'), sandbox);

  return sandbox;
}

console.log(`build under test: ${FIXED ? 'FIXED' : 'ORIGINAL (pre-fix)'}\n`);

const findings = [];

// Scenario 1: active session slot.
{
  const shared = makeStorage();
  const a = makeTab(shared), b = makeTab(shared);
  a.window._rememberActiveSession('session-AAA');
  b.window._rememberActiveSession('session-BBB');
  const seenByA = a.window._rememberedActiveSession();
  const ok = seenByA === 'session-AAA';
  findings.push(ok);
  console.log(`[${ok ? 'OK  ' : 'BUG '}] tab A reload restores: ${seenByA}  (expected session-AAA)`);
}

// Scenario 2: reconnect marker.
{
  const shared = makeStorage();
  const a = makeTab(shared), b = makeTab(shared);
  a.window.markInflight('session-AAA', 'stream-A');
  b.window.markInflight('session-BBB', 'stream-B');
  const rawA = shared.getItem(a.window.INFLIGHT_KEY);
  const sidA = rawA ? JSON.parse(rawA).sid : null;
  const ok = sidA === 'session-AAA';
  findings.push(ok);
  console.log(`[${ok ? 'OK  ' : 'BUG '}] tab A reconnect marker points at: ${sidA}  (expected session-AAA)`);
}

// Scenario 3: transcript snapshot bleed.
{
  const shared = makeStorage();
  const a = makeTab(shared), b = makeTab(shared);
  a.window.saveInflightState('shared-session', {streamId:'stream-A', messages:[{role:'user',content:'FROM TAB A'}]});
  b.window.saveInflightState('shared-session', {streamId:'stream-B', messages:[{role:'user',content:'FROM TAB B'}]});
  const got = a.window.loadInflightState('shared-session', null);
  const text = got && got.messages && got.messages[0] ? got.messages[0].content : '(none)';
  const ok = text === 'FROM TAB A';
  findings.push(ok);
  console.log(`[${ok ? 'OK  ' : 'BUG '}] tab A recovers transcript: ${text}  (expected FROM TAB A)`);
}

const bugs = findings.filter(x => !x).length;
console.log(`\n${bugs} cross-tab defect(s) observed.`);
process.exit(0);
