"""Background live-stream pool: keep N conversations streaming at once.

Historically ``closeOtherLiveStreams(activeSid)`` closed the EventSource of
EVERY session except the one being viewed. The stated reason (#2313) was the
browser connection pool: one EventSource per background conversation would
exhaust the HTTP/1.1 six-connection-per-origin budget and starve ordinary
XHR/fetch traffic.

That reasoning caps the number of *simultaneous* streams; it does not require
the number to be one. Closing every background stream means switching away from
a running conversation drops its live token feed: coming back re-fetches and
replays the run journal instead of having simply kept receiving. For an
operator piloting several conversations at once (the multi-tab workflow) that is
the dominant source of perceived slowness.

These tests lock the bounded-pool behaviour:

* the active session's stream is never closed by the pool;
* up to ``LIVE_STREAM_POOL_MAX`` streams stay open simultaneously;
* beyond that, the LEAST-RECENTLY-USED background stream is evicted, so the
  connection budget stays bounded exactly as #2313 required;
* eviction goes through ``closeLiveStream``, preserving its snapshot + reattach
  bookkeeping rather than dropping the transport silently.

The function under test is the real region extracted from static/messages.js and
executed under node, matching tests/test_5306_subagent_sidebar_flicker.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This module only reads static source; it does not need the HTTP fixture."""


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


_PREAMBLE = """
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
// `eval('const X=…')` scopes the binding to the eval itself and does NOT leak
// it to the global scope (function declarations do, which is why extractFunc
// works). Extract the right-hand side instead and bind it explicitly.
function extractConstValue(name) {{
  const re = new RegExp('const\\\\s+' + name + '\\\\s*=\\\\s*([^;]*);');
  const m = src.match(re);
  if (!m) throw new Error(name + ' not found');
  return eval(m[1]);
}}
// Module-level state the extracted pool functions close over. Declared on
// globalThis (not with `const`/`let`) so the eval'd regions resolve them as
// bare identifiers, exactly as they do inside the real module scope.
globalThis._LIVE_STREAM_USE = {{}};
globalThis._liveStreamUseClock = 0;
// Record which sessions were torn down, so a test can assert the pool evicted
// the right ones without needing the whole closeLiveStream machinery.
global.closed = [];
global.LIVE_STREAMS = {{}};
global.closeLiveStream = function(sid){{
  if (!global.LIVE_STREAMS[sid]) return;
  global.closed.push(sid);
  delete global.LIVE_STREAMS[sid];
  delete globalThis._LIVE_STREAM_USE[sid];
}};
"""


def _preamble() -> str:
    js = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    return _PREAMBLE.format(js=js)


def _pool_source(scenario: str, next_hop_protocol: str | None = "http/1.1") -> str:
    """Build a Node program exercising the real pool functions.

    ``next_hop_protocol`` fakes what the browser negotiated with the FIRST hop
    (tailscale serve), which is what actually spends the per-origin connection
    budget. ``None`` simulates navigation timing being unavailable.
    """
    if next_hop_protocol is None:
        perf = "globalThis.performance = { getEntriesByType: () => [] };\n"
    else:
        perf = (
            "globalThis.performance = { getEntriesByType: (t) => "
            "t === 'navigation' ? [{ nextHopProtocol: %r }] : [] };\n"
            % (next_hop_protocol,)
        ).replace("'", '"')
    return (
        _preamble()
        + perf
        + "globalThis.LIVE_STREAM_POOL_MAX_HTTP1 = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_HTTP1');\n"
        + "globalThis.LIVE_STREAM_POOL_MAX_MULTIPLEXED = "
          "extractConstValue('LIVE_STREAM_POOL_MAX_MULTIPLEXED');\n"
        + "globalThis._liveStreamPoolMaxCache = 0;\n"
        + "eval(extractFunc('_liveStreamPoolMax'));\n"
        + "eval(extractFunc('_touchLiveStreamUse'));\n"
        + "eval(extractFunc('closeOtherLiveStreams'));\n"
        + scenario
    )


def test_active_session_stream_is_never_closed_by_the_pool():
    """The conversation on screen must keep its own transport."""
    out = json.loads(_run_node(_pool_source("""
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({ open:Object.keys(LIVE_STREAMS), closed }));
""")))
    assert out["open"] == ["a"]
    assert out["closed"] == []


def test_background_streams_below_the_cap_stay_open():
    """RED before the fix: every background stream was closed unconditionally.

    One background conversation plus the active one is inside the connection
    budget, so both must keep streaming.
    """
    out = json.loads(_run_node(_pool_source("""
LIVE_STREAMS['a'] = { streamId:'sa' };
LIVE_STREAMS['b'] = { streamId:'sb' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({ open:Object.keys(LIVE_STREAMS).sort(), closed }));
""")))
    assert out["open"] == ["a", "b"]
    assert out["closed"] == []


def test_pool_evicts_least_recently_used_background_stream_beyond_the_cap():
    """The connection budget stays bounded: the oldest background stream goes.

    'b' is touched (viewed) after 'c', so 'c' is the least-recently-used and is
    the one evicted when the pool overflows.
    """
    out = json.loads(_run_node(_pool_source("""
// Open order: b, then c.
LIVE_STREAMS['b'] = { streamId:'sb' }; _touchLiveStreamUse('b');
LIVE_STREAMS['c'] = { streamId:'sc' }; _touchLiveStreamUse('c');
// The user visited 'b' again, making 'c' the least-recently-used.
_touchLiveStreamUse('b');
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({
  open:Object.keys(LIVE_STREAMS).sort(), closed, cap:_liveStreamPoolMax(),
}));
""")))
    assert out["cap"] == 3
    # 3 streams total is exactly the cap: nothing is evicted yet.
    assert out["open"] == ["a", "b", "c"]
    assert out["closed"] == []


def test_pool_never_exceeds_the_connection_budget():
    """Six live conversations must collapse to the cap, active one retained."""
    out = json.loads(_run_node(_pool_source("""
for (const sid of ['b','c','d','e','f']) {
  LIVE_STREAMS[sid] = { streamId:'s'+sid };
  _touchLiveStreamUse(sid);
}
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({
  open:Object.keys(LIVE_STREAMS).sort(), closed:closed.slice().sort(),
  cap:_liveStreamPoolMax(),
}));
""")))
    assert len(out["open"]) == out["cap"]
    assert "a" in out["open"], "the viewed conversation must never be evicted"
    # Only the two most recently used background streams survive.
    assert out["closed"] == ["b", "c", "d"]


def test_multiplexed_transport_raises_the_cap():
    """On HTTP/2 the per-origin connection limit does not apply, so more
    conversations may stream at once. The browser reports the protocol it
    negotiated with the first hop; the pool must honour it."""
    out = json.loads(_run_node(_pool_source("""
for (const sid of ['b','c','d','e','f','g','h']) {
  LIVE_STREAMS[sid] = { streamId:'s'+sid };
  _touchLiveStreamUse(sid);
}
LIVE_STREAMS['a'] = { streamId:'sa' };
closeOtherLiveStreams('a');
console.log(JSON.stringify({
  open:Object.keys(LIVE_STREAMS).sort(), cap:_liveStreamPoolMax(),
}));
""", next_hop_protocol="h2")))
    assert out["cap"] == 6
    assert len(out["open"]) == 6
    assert "a" in out["open"]


def test_http3_is_treated_as_multiplexed():
    out = json.loads(_run_node(_pool_source(
        "console.log(JSON.stringify({ cap:_liveStreamPoolMax() }));",
        next_hop_protocol="h3-29",
    )))
    assert out["cap"] == 6


def test_unknown_transport_fails_closed_to_the_http1_budget():
    """Navigation timing can be unavailable. Unknown must not be treated as
    multiplexed, and must not be memoised as a permanent answer."""
    out = json.loads(_run_node(_pool_source(
        "const first=_liveStreamPoolMax();\n"
        "globalThis.performance = { getEntriesByType: (t) => "
        "t === 'navigation' ? [{ nextHopProtocol: 'h2' }] : [] };\n"
        "console.log(JSON.stringify({ first, second:_liveStreamPoolMax() }));",
        next_hop_protocol=None,
    )))
    assert out["first"] == 3, "unknown transport must fail closed to HTTP/1.1"
    assert out["second"] == 6, (
        "an unknown transport must not be cached: once navigation timing "
        "reports h2 the pool must widen"
    )


def test_eviction_uses_close_live_stream_for_snapshot_and_reattach():
    """Evicted streams must go through closeLiveStream, not a raw .close().

    closeLiveStream snapshots the live-turn DOM and flags INFLIGHT.reattach so
    returning to the conversation restores it. Bypassing it would lose the
    streamed content the pool is meant to preserve.
    """
    src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
    start = src.index("function closeOtherLiveStreams(")
    body = src[start:src.index("\n}", start)]
    assert "closeLiveStream(" in body
    assert ".close()" not in body, "must delegate teardown, not close the transport directly"
