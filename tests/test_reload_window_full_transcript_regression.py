"""Reload-window regression: a same-session refresh must not refetch the whole transcript.

`_messageReloadLimitForSession()` decides how wide a window a same-session
force-reload asks the server for. Two inputs made it return `null`, and a null
window means the bare `/api/session?messages=1` request — the FULL transcript:

1. `hint.truncated === false` (the whole conversation is already loaded, which
   is the normal state for any conversation shorter than the initial window);
2. a hint width above the server `msg_limit` ceiling (`_msgLimitMax`), where the
   caller deliberately dropped the window to avoid the backend clamping it and
   silently shrinking an already-loaded transcript (#6154).

Both cases are hit by the returning-from-background path
(`refreshActiveSessionIfExternallyUpdated` -> `loadSession({force:true})`), so
coming back to a conversation after switching apps refetched every message to
display the one or two that actually arrived.

The fix keeps the anti-shrink invariant that motivated the null: never ask for a
window NARROWER than what is already on screen. When the desired width exceeds
the ceiling we now clamp to the ceiling and only fall back to the full
transcript if even the ceiling would shrink the visible transcript.

These tests execute the real functions under node instead of asserting on source
strings, so they fail on the pre-fix behaviour rather than on a refactor.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _slice_function(name: str) -> str:
    """Return the source text of one top-level `function name(...)` block."""
    start = SESSIONS_JS.index(f"function {name}(")
    depth = 0
    i = SESSIONS_JS.index("{", start)
    for pos in range(i, len(SESSIONS_JS)):
        ch = SESSIONS_JS[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start : pos + 1]
    raise AssertionError(f"unbalanced braces while slicing {name}")


def _harness(cases: list[dict]) -> str:
    """Run _messageReloadLimitForSession + the caller's bounding step per case.

    The bounding expression is the real one from _ensureMessagesLoaded; it is
    reproduced here because that function is a large async body that touches the
    network. `resolved` is the msg_limit actually sent to the server, and null
    means "no msg_limit param" -> full transcript.

    The cold-load constant is read from the file instead of hard-coded:
    `_INITIAL_TAIL_MSG_LIMIT` is a local addition and upstream still falls back
    to `_INITIAL_MSG_LIMIT`, so pinning a literal here would make the test
    depend on an unrelated patch rather than on the reload-window behaviour.
    """
    fn = _slice_function("_messageReloadLimitForSession")
    tail_const = (
        "const _INITIAL_TAIL_MSG_LIMIT = 8;"
        if "_INITIAL_TAIL_MSG_LIMIT" in SESSIONS_JS
        else ""
    )
    return f"""
const _INITIAL_MSG_LIMIT = 30;
{tail_const}
let _msgLimitMax = 500;
let _sameSessionForceReloadHint = null;
let S = {{}};

{fn}

const cases = {json.dumps(cases)};
const out = [];
for (const c of cases) {{
  _msgLimitMax = c.ceiling;
  _sameSessionForceReloadHint = c.hint;
  S = {{ session: {{ session_id: c.sid, message_count: c.server_message_count }} }};
  const requested = _messageReloadLimitForSession(c.sid);
  const resolved = (requested && requested > 0) ? requested : null;
  out.push({{ name: c.name, requested, resolved }});
}}
console.log(JSON.stringify(out));
"""


def _cold_load_width() -> int:
    """Width used when there is no width hint (cold load), per the live source."""
    return 8 if "_INITIAL_TAIL_MSG_LIMIT" in SESSIONS_JS else 30


def _resolve(cases: list[dict]) -> dict:
    rows = json.loads(_run_node(_harness(cases)))
    return {row["name"]: row for row in rows}


def test_fully_loaded_conversation_does_not_refetch_whole_transcript():
    """A short conversation held entirely in memory must reload only a tail window.

    Pre-fix: hint.truncated === false returned null -> the client refetched all
    285 messages (measured at 1.76 MB / 406 ms on a real session) to pick up the
    two that arrived while the tab was hidden.
    """
    got = _resolve(
        [
            {
                "name": "fully_loaded",
                "sid": "s1",
                "ceiling": 500,
                "server_message_count": 287,
                "hint": {
                    "session_id": "s1",
                    "loaded_renderable_count": 285,
                    "loaded_message_count": 285,
                    "message_count": 285,
                    "truncated": False,
                },
            }
        ]
    )["fully_loaded"]

    assert got["resolved"] is not None, (
        "a fully-loaded conversation still asks for the FULL transcript on refresh"
    )
    # Never narrower than what is already rendered, or the transcript shrinks.
    assert got["resolved"] >= 285
    assert got["resolved"] <= 500


def test_hint_above_ceiling_clamps_instead_of_refetching_everything():
    """An over-ceiling DESIRED width must clamp to the ceiling, not drop the window.

    The visible transcript (480 rows) still fits in one server window, but the
    desired width grows past the ceiling because 60 messages arrived while the
    tab was hidden. Pre-fix, any width > _msgLimitMax fell back to the full
    transcript: 11 MB / 2.2 s on a 9.5k-message session to surface 60 new rows.
    Clamping keeps every visible row and stays bounded.
    """
    got = _resolve(
        [
            {
                "name": "above_ceiling",
                "sid": "s2",
                "ceiling": 500,
                "server_message_count": 9601,
                "hint": {
                    "session_id": "s2",
                    "loaded_renderable_count": 480,
                    "loaded_message_count": 480,
                    "message_count": 9541,
                    "truncated": True,
                },
            }
        ]
    )["above_ceiling"]

    assert got["resolved"] is not None, (
        "an over-ceiling desired width still refetches the FULL transcript"
    )
    assert got["resolved"] == 500, "should clamp to the advertised ceiling"


def test_visible_transcript_wider_than_ceiling_keeps_full_reload():
    """The anti-shrink invariant (#6154) still wins when the ceiling cannot hold the view.

    If more rows are on screen than the server will ever return in one window,
    clamping WOULD drop already-loaded rows. That case must keep the full
    transcript path — fail closed toward correctness, not toward speed.
    """
    got = _resolve(
        [
            {
                "name": "cannot_clamp",
                "sid": "s3",
                "ceiling": 500,
                "server_message_count": 9543,
                "hint": {
                    "session_id": "s3",
                    "loaded_renderable_count": 900,
                    "loaded_message_count": 900,
                    "message_count": 9541,
                    "truncated": True,
                },
            }
        ]
    )["cannot_clamp"]

    assert got["resolved"] is None, (
        "clamping below the visible row count would silently shrink the transcript"
    )


def test_ordinary_truncated_window_is_unchanged():
    """The already-correct common path must keep its exact behaviour."""
    got = _resolve(
        [
            {
                "name": "ordinary",
                "sid": "s4",
                "ceiling": 500,
                "server_message_count": 9543,
                "hint": {
                    "session_id": "s4",
                    "loaded_renderable_count": 60,
                    "loaded_message_count": 60,
                    "message_count": 9541,
                    "truncated": True,
                },
            },
            {
                "name": "cold",
                "sid": "s5",
                "ceiling": 500,
                "server_message_count": 400,
                "hint": None,
            },
        ]
    )
    assert got["ordinary"]["resolved"] == 62
    assert got["cold"]["resolved"] == _cold_load_width()


def test_ceiling_is_read_from_server_metadata():
    """A server advertising a different ceiling must be honoured (no mirrored const)."""
    got = _resolve(
        [
            {
                "name": "low_ceiling",
                "sid": "s6",
                "ceiling": 120,
                "server_message_count": 9543,
                "hint": {
                    "session_id": "s6",
                    "loaded_renderable_count": 118,
                    "loaded_message_count": 118,
                    "message_count": 9541,
                    "truncated": True,
                },
            }
        ]
    )["low_ceiling"]
    assert got["resolved"] == 120
