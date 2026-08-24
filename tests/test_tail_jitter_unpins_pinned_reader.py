"""Non-regression: browser tail jitter must not unpin a pinned reader.

Reproduction (long transcripts, desktop Chromium): on opening a long
conversation the reader is placed AT the tail, then the browser itself moves
scrollTop up by ~8px with NO scrollHeight/clientHeight change and with no
scrollTop write from the app (verified against scrollTop, scrollIntoView,
focus, scrollTo and scrollBy -- it is a layout-settle artifact).

The scroll listener's direction test (`top < _lastScrollTop - 2`) read that
artifact as an upward user scroll and latched `_messageUserUnpinned = true`
on a reader who never touched anything. Auto-follow then stayed off for the
whole session, and later renders restored the SEMANTIC viewport anchor
instead of the tail -- landing the reader in the middle of the conversation.

The guard ignores an upward delta only when ALL of these hold:
  - the drift is small (<= MESSAGE_TAIL_JITTER_MAX_DELTA_PX),
  - the reader is still visually AT the bottom
    (<= MESSAGE_TAIL_JITTER_MAX_BOTTOM_PX),
  - there is no real input intent (wheel / touch / key / scrollbar drag /
    non-message scroll).

A genuine scroll-up always carries input intent, so it still unpins.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _scroll_listener_block() -> str:
    """Return the rAF callback inside the messages scroll listener."""
    anchor = "el.addEventListener('scroll'"
    start = UI_JS.index(anchor)
    raf_start = UI_JS.index("requestAnimationFrame", start)
    brace = UI_JS.index("{", raf_start)
    depth = 0
    for i in range(brace, len(UI_JS)):
        ch = UI_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[brace : i + 1]
    raise AssertionError("scroll listener rAF callback not found")


def test_tail_jitter_thresholds_are_defined():
    """Both bounds must exist as explicit, reviewable constants."""
    assert "const MESSAGE_TAIL_JITTER_MAX_BOTTOM_PX=" in UI_JS
    assert "const MESSAGE_TAIL_JITTER_MAX_DELTA_PX=" in UI_JS


def test_tail_jitter_thresholds_stay_sub_scroll():
    """The window must stay far below a deliberate scroll gesture."""
    import re

    for name in (
        "MESSAGE_TAIL_JITTER_MAX_BOTTOM_PX",
        "MESSAGE_TAIL_JITTER_MAX_DELTA_PX",
    ):
        m = re.search(rf"const {name}=(\d+);", UI_JS)
        assert m, f"{name} must be a plain integer constant"
        value = int(m.group(1))
        assert 0 < value <= 32, (
            f"{name}={value} is too wide: the guard must only absorb browser "
            "layout drift, never a real reader scroll."
        )


def test_moved_up_ignores_tail_jitter():
    """The direction test must exclude the jitter artifact."""
    block = _scroll_listener_block()
    assert "_tailJitter" in block, (
        "The scroll listener must compute a tail-jitter flag so browser "
        "layout drift at the bottom is not read as an upward user scroll."
    )
    assert "const movedUp=!grew&&!_tailJitter&&" in block, (
        "movedUp must exclude tail jitter, otherwise an ~8px browser nudge "
        "at the tail latches _messageUserUnpinned and kills auto-follow."
    )


def test_tail_jitter_requires_reader_at_bottom():
    """Far from the tail, an upward scroll must always unpin."""
    block = _scroll_listener_block()
    assert "bottomDistance>MESSAGE_TAIL_JITTER_MAX_BOTTOM_PX" in block
    assert "delta>0&&delta<=MESSAGE_TAIL_JITTER_MAX_DELTA_PX" in block


def test_tail_jitter_yields_to_every_real_input_intent():
    """Any genuine reader input must bypass the guard and unpin normally."""
    block = _scroll_listener_block()
    start = block.index("_tailJitter")
    end = block.index("const movedUp=", start)
    guard = block[start:end]
    for intent in (
        "_scrollbarDragActive",
        "_recentMessageWheelIntent",
        "_recentMessageTouchScrollIntent",
        "_recentMessageKeyScrollIntent",
        "_recentNonMessageScrollIntent",
    ):
        assert intent in guard, (
            f"The tail-jitter guard must yield to {intent}: a real scroll-up "
            "must keep unpinning the reader."
        )
