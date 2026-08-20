"""Behavioural coverage for instant session switching (#fastnav).

These tests execute the real `_apiSessionNav` / `_prefetchSessionForNav` /
`invalidateSessionNavCache` sources out of `static/sessions.js` inside a Node
harness. They assert observable behaviour — how many network calls happen, what
payload the caller receives, when the cache is dropped — rather than the
presence of source strings, so a refactor that preserves behaviour keeps
passing and a regression that breaks it fails.

Covered state-space:
  * cold miss -> fetch, and the result is retained for the NEXT visit;
  * warm hit within the revalidation TTL -> zero network calls;
  * stale-but-usable hit -> instant payload AND a background revalidation;
  * beyond max age -> no stale paint at all;
  * streaming row -> cache never consulted and any entry dropped (SSE owns it);
  * mutation (send / turn end / detach) -> entry invalidated;
  * cross-tab mutation -> other tabs drop their entry via BroadcastChannel;
  * LRU capacity follows the live-stream pool instead of a private constant.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute the session-nav cache harness",
)


def _extract(source: str, start: str, end: str) -> str:
    """Slice [start, end) out of a JS source file, failing loudly if absent."""
    try:
        a = source.index(start)
    except ValueError:  # pragma: no cover - diagnostic path
        raise AssertionError(f"anchor not found in source: {start!r}")
    try:
        b = source.index(end, a + len(start))
    except ValueError:  # pragma: no cover - diagnostic path
        raise AssertionError(f"end anchor not found after {start!r}: {end!r}")
    return source[a:b]


# The exact production slice under test: from the cache-sizing helper through
# the end of _apiSessionNav. Pulling the real source (instead of a copy) is what
# makes these tests regression-proof.
NAV_CACHE_SOURCE = _extract(
    SESSIONS_JS,
    "const _SESSION_NAV_CACHE_FALLBACK_MAX",
    "async function _ensureMessagesLoaded",
)


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const navSource = fs.readFileSync(process.argv[3], 'utf8');

// ---- minimal environment the extracted slice closes over --------------------
let NOW = 1000000;
const _origNow = Date.now;
Date.now = () => NOW;

const calls = [];               // every api() URL actually requested
let failNext = false;

function api(url, opts){
  calls.push(url);
  if (failNext) { failNext = false; return Promise.reject(new Error('boom')); }
  // Payload carries a generation stamp so the test can tell a cached payload
  // apart from a freshly fetched one.
  return Promise.resolve({ url, gen: calls.length });
}

const S = { session: null };
let STREAMING = new Set();
let POOL_MAX = 30;
function _liveStreamPoolMax(){ return POOL_MAX; }

// _sessionNavRowIsStreaming reads the DOM in production; stub the query it uses.
const document = {
  querySelectorAll(){
    return Array.from(STREAMING).map(sid => ({ dataset: { sid } }));
  },
};

const _INITIAL_TAIL_MSG_LIMIT = 8;

// Cross-tab channel: emulate a second tab sharing the same channel name.
const CHANNELS = {};
class BroadcastChannel {
  constructor(name){
    this.name = name;
    this.onmessage = null;
    (CHANNELS[name] = CHANNELS[name] || []).push(this);
  }
  postMessage(data){
    for (const c of (CHANNELS[this.name] || [])) {
      if (c !== this && typeof c.onmessage === 'function') c.onmessage({ data });
    }
  }
  close(){}
}
const window = {};

// ---- load the real production slice ----------------------------------------
// `eval` of a slice containing top-level `const`/`function` keeps those
// bindings inside eval's own scope, so capture the two internals the capacity
// assertions inspect via the eval completion value. Re-declaring them with the
// same names here would collide with the slice's own declarations.
const _internals = eval(navSource + '\n;({cache:_sessionNavCache, maxFn:_sessionNavCacheMax});');
const navCache = _internals.cache;
const navCacheMax = _internals.maxFn;

const flush = () => new Promise(r => setImmediate(r));

async function main(){
  const out = {};
  const base = (sid) => `/api/session?session_id=${encodeURIComponent(sid)}`;
  const tail = (sid) => `${base(sid)}&messages=1&resolve_model=0&msg_limit=${_INITIAL_TAIL_MSG_LIMIT}&expand_renderable=1`;

  if (scenario.case === 'cold_then_warm') {
    // First visit: must hit the network. Second visit: must NOT.
    const a = await _apiSessionNav('s1', tail('s1'));
    await flush();
    const afterFirst = calls.length;
    const b = await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.afterFirst = afterFirst;
    out.afterSecond = calls.length;
    out.sameGeneration = (a.gen === b.gen);
  }

  if (scenario.case === 'prefetch_then_click') {
    _prefetchSessionForNav('s1');
    const duringPrefetch = calls.length;
    const got = await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.duringPrefetch = duringPrefetch;
    out.afterClick = calls.length;
    out.gotPayload = !!got;
  }

  if (scenario.case === 'stale_revalidate') {
    await _apiSessionNav('s1', tail('s1'));
    await flush();
    const afterSeed = calls.length;
    NOW += 60 * 1000;                      // past TTL, well under max age
    const served = await _apiSessionNav('s1', tail('s1'));
    const immediatelyAfter = calls.length; // background refresh may be in flight
    await flush();
    out.afterSeed = afterSeed;
    out.servedGen = served.gen;            // 1 => served from cache, not refetched
    out.afterServe = immediatelyAfter;
    out.afterFlush = calls.length;         // must have revalidated behind
  }

  if (scenario.case === 'beyond_max_age') {
    await _apiSessionNav('s1', tail('s1'));
    await flush();
    NOW += 60 * 60 * 1000;                 // way past max age
    const served = await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.servedGen = served.gen;            // must be a fresh fetch, not gen 1
    out.totalCalls = calls.length;
  }

  if (scenario.case === 'streaming_never_cached') {
    STREAMING.add('s1');
    await _apiSessionNav('s1', tail('s1'));
    await flush();
    const first = calls.length;
    await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.first = first;
    out.second = calls.length;             // must fetch every time
    // A prefetch must also refuse to warm a streaming row.
    const before = calls.length;
    _prefetchSessionForNav('s1');
    out.prefetchCalls = calls.length - before;
  }

  if (scenario.case === 'invalidate') {
    await _apiSessionNav('s1', tail('s1'));
    await flush();
    const seeded = calls.length;
    invalidateSessionNavCache('s1');
    const served = await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.seeded = seeded;
    out.servedGen = served.gen;            // fresh fetch, not the cached gen 1
    out.total = calls.length;
  }

  if (scenario.case === 'cross_tab') {
    // Tab A caches s1; tab B (same channel) invalidates it.
    await _apiSessionNav('s1', tail('s1'));
    await flush();
    const other = new BroadcastChannel('hermes-webui-session-nav');
    other.postMessage({ sid: 's1' });
    const served = await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.servedGen = served.gen;            // must be refetched after remote invalidation
    out.total = calls.length;
  }

  if (scenario.case === 'capacity_follows_pool') {
    POOL_MAX = scenario.poolMax;
    for (let i = 0; i < scenario.poolMax + 5; i++) {
      await _apiSessionNav('s' + i, tail('s' + i));
      await flush();
    }
    out.size = navCache.size;
    out.max = navCacheMax();
    // The oldest entries must have been evicted, the newest retained.
    out.hasOldest = navCache.has('s0');
    out.hasNewest = navCache.has('s' + (scenario.poolMax + 4));
  }

  if (scenario.case === 'failed_prefetch_recovers') {
    failNext = true;
    _prefetchSessionForNav('s1');
    await flush(); await flush();
    const served = await _apiSessionNav('s1', tail('s1'));
    await flush();
    out.served = !!served;                 // must still resolve via a real fetch
  }

  Date.now = _origNow;
  process.stdout.write(JSON.stringify(out));
}

main().catch(e => { process.stdout.write(JSON.stringify({ error: String(e && e.stack || e) })); });
"""


def _run(scenario: dict, tmp_path: Path) -> dict:
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    src = tmp_path / "nav.js"
    src.write_text(NAV_CACHE_SOURCE, encoding="utf-8")
    proc = subprocess.run(
        [str(NODE), str(driver), json.dumps(scenario), str(src)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}\n{proc.stdout}"
    data = json.loads(proc.stdout)
    assert "error" not in data, data.get("error")
    return data


def test_second_visit_serves_from_cache_without_network(tmp_path):
    """The regression that made switching slow: every visit re-fetched.

    Before the fix _apiSessionNav only consumed a single-use prefetch promise
    and never retained the settled payload, so a direct click always paid a
    round-trip and returning to a conversation paid it again.
    """
    out = _run({"case": "cold_then_warm"}, tmp_path)
    assert out["afterFirst"] == 1, "a cold visit must fetch exactly once"
    assert out["afterSecond"] == 1, "returning must not issue another request"
    assert out["sameGeneration"] is True, "the retained payload must be reused"


def test_prefetch_is_awaited_instead_of_refetched(tmp_path):
    out = _run({"case": "prefetch_then_click"}, tmp_path)
    assert out["duringPrefetch"] == 2, "prefetch warms the meta + tail requests"
    assert out["afterClick"] == 2, "the click must await the prefetch, not refetch"
    assert out["gotPayload"] is True


def test_stale_entry_paints_instantly_and_revalidates(tmp_path):
    """Past the TTL the user still gets an instant paint, but truth is refreshed."""
    out = _run({"case": "stale_revalidate"}, tmp_path)
    assert out["afterSeed"] == 1
    assert out["servedGen"] == 1, "must serve the cached payload immediately"
    assert out["afterFlush"] == 2, "must revalidate against the server behind the paint"


def test_entry_beyond_max_age_is_not_painted(tmp_path):
    """Freshness has a hard ceiling: too-old state is never shown."""
    out = _run({"case": "beyond_max_age"}, tmp_path)
    assert out["servedGen"] != 1, "an over-age entry must not be served"
    assert out["totalCalls"] == 2


def test_streaming_row_bypasses_cache_entirely(tmp_path):
    """A live turn is owned by SSE; cache must never answer for it."""
    out = _run({"case": "streaming_never_cached"}, tmp_path)
    assert out["first"] == 1
    assert out["second"] == 2, "a streaming row must refetch every time"
    assert out["prefetchCalls"] == 0, "a streaming row must not be prefetched"


def test_invalidation_forces_a_refetch(tmp_path):
    out = _run({"case": "invalidate"}, tmp_path)
    assert out["seeded"] == 1
    assert out["servedGen"] != 1, "invalidated entry must not be served"
    assert out["total"] == 2


def test_cross_tab_invalidation_drops_local_entry(tmp_path):
    """Several Chrome tabs: a mutation in one must not leave others stale."""
    out = _run({"case": "cross_tab"}, tmp_path)
    assert out["servedGen"] != 1, "remote invalidation must drop the cached payload"
    assert out["total"] == 2


@pytest.mark.parametrize("pool_max", [4, 30])
def test_capacity_tracks_live_stream_pool(tmp_path, pool_max):
    """Cache capacity must follow the pool, not a private constant of its own."""
    out = _run({"case": "capacity_follows_pool", "poolMax": pool_max}, tmp_path)
    assert out["max"] == pool_max
    assert out["size"] == pool_max, "LRU must bound the cache at the pool size"
    assert out["hasOldest"] is False, "oldest entry must be evicted"
    assert out["hasNewest"] is True, "most recent entry must be retained"


def test_failed_prefetch_does_not_poison_navigation(tmp_path):
    out = _run({"case": "failed_prefetch_recovers"}, tmp_path)
    assert out["served"] is True, "a failed prefetch must fall back to a real fetch"


# --- invalidation wiring -----------------------------------------------------
# The harness proves invalidateSessionNavCache() works; these assert it is
# actually CALLED at each of the three server-side mutation points. Without
# this wiring a conversation could paint from cache while missing its newest
# turn, which is a correctness bug, not a performance one.

def test_send_invalidates_before_the_turn_starts():
    body = _extract(MESSAGES_JS, "  const activeSid=S.session.session_id;\n  _sendInProgressSid=activeSid;", "// Salvage of #4750")
    assert "invalidateSessionNavCache(activeSid)" in body


def test_turn_completion_invalidates():
    body = _extract(MESSAGES_JS, "function _clearOwnerInflightState(){", "function _isMarkerOnlyAssistantMessage")
    assert "invalidateSessionNavCache(activeSid)" in body


def test_stream_detach_invalidates():
    body = _extract(MESSAGES_JS, "function closeLiveStream(sessionId, streamId, source){", "_resumeSessionStreamAfterLiveChat(sessionId);")
    assert "invalidateSessionNavCache(sessionId)" in body


# --- touch prefetch ----------------------------------------------------------

def test_touch_press_warms_the_cache_in_the_pwa():
    """Touch has no hover: without a pointerdown warm-up the PWA never prefetched."""
    body = _extract(SESSIONS_JS, "el.onpointerenter=(e)=>{", "el.onpointercancel=")
    assert "addEventListener('pointerdown'" in body
    assert "e.pointerType!=='touch'" in body, "touch-only: mouse already warms on enter"
    assert "_prefetchSessionForNav(s.session_id)" in body
    assert "{passive:true}" in body, "must not block scrolling"


# --- background tool accumulation -------------------------------------------

def test_background_tool_events_are_recorded_before_visibility_check():
    """A backgrounded conversation must keep accumulating its tool cards.

    Previously both handlers returned early when the pane was not visible, so
    every tool call of a background conversation was lost and switching back
    required a full journal replay to reconstruct the turn.
    """
    for evt, end in (("source.addEventListener('tool',e=>{", "source.addEventListener('tool_complete'"),
                     ("source.addEventListener('tool_complete',e=>{", "_maybeNotifyPersistentStateSaved(tc);")):
        body = _extract(MESSAGES_JS, evt, end)
        upsert = body.index("upsertLiveToolCall(")
        visibility = body.index("S.session.session_id!==activeSid")
        assert upsert < visibility, (
            f"{evt}: the tool call must be recorded into INFLIGHT before the "
            "pane-visibility early return, or background work is dropped"
        )
        assert "_ownsActiveStreamOrBackground()" in body, (
            f"{evt}: must use the shared background-aware stream ownership guard"
        )
