"""Regression test: live run-status footer must follow a successful fallback swap.

Bug: the live footer (`run_meta` SSE, rendered by static/ui.js
`_renderLiveRunStatusContent`) is announced exactly once at turn start with
the originally requested model/provider. When the primary model is exhausted
(e.g. Codex `gpt-5.6-sol` hits a 429 usage-limit) and hermes-agent's
`_try_activate_fallback` swaps `agent.model`/`agent.provider` in place, the
one-shot success notice `agent._emit_pending_fallback_notice()` fires
("Switched to fallback model: X via P1 → Y via P2") but the old
`_is_fallback_lifecycle_message` matcher did not recognise that past-tense
wording, so it fell through to "dropped" (matched neither the compression,
lease-wait, nor fallback branches) — and `run_meta` was never re-emitted.
Anthony's session footer kept showing "gpt-5.6-sol" for the whole turn while
claude-fable-5 was the model actually answering and billed in state.db.

Covers:
- `_is_fallback_lifecycle_message` recognises the past-tense success notice
  (so it still surfaces as a 'warning' toast, unchanged prior behavior);
- a new `_is_fallback_switch_succeeded_message` predicate distinguishes the
  one-shot success line from the buffered pre-swap "switching to fallback"
  attempt line (which fires even for candidates that then fail);
- the success line, and *only* the success line, triggers a `run_meta`
  re-announce carrying the agent's post-swap model/provider/effort.
"""

from __future__ import annotations

from api.streaming import (
    _is_fallback_lifecycle_message,
    _is_fallback_switch_succeeded_message,
)


class TestFallbackNoticeWordingRecognised:
    def test_pre_swap_attempt_line_matches_lifecycle_warning(self):
        assert _is_fallback_lifecycle_message(
            "lifecycle",
            "🔄 Primary model failed — switching to fallback: claude-fable-5 via anthropic",
        )

    def test_post_swap_success_line_matches_lifecycle_warning(self):
        # This is the wording that was previously dropped.
        assert _is_fallback_lifecycle_message(
            "lifecycle",
            "🔄 Switched to fallback model: gpt-5.6-sol via openai-codex "
            "→ claude-fable-5 via anthropic",
        )

    def test_unrelated_lifecycle_message_does_not_match(self):
        assert not _is_fallback_lifecycle_message("lifecycle", "Compressing context…")

    def test_non_lifecycle_kind_never_matches(self):
        assert not _is_fallback_lifecycle_message(
            "warn", "🔄 Switched to fallback model: a via b → c via d"
        )


class TestFallbackSwitchSucceededPredicate:
    def test_success_line_is_the_succeeded_predicate(self):
        assert _is_fallback_switch_succeeded_message(
            "lifecycle",
            "🔄 Switched to fallback model: gpt-5.6-sol via openai-codex "
            "→ claude-fable-5 via anthropic",
        )

    def test_pre_swap_attempt_line_is_not_the_succeeded_predicate(self):
        # The buffered "switching to fallback" attempt line fires once per
        # candidate tried, including ones that then fail (e.g. the
        # xai/provider-not-configured miss before landing on xai-oauth).
        # Re-announcing run_meta on every attempt would flicker the footer
        # through failed candidates; only the durable success notice should.
        assert not _is_fallback_switch_succeeded_message(
            "lifecycle",
            "🔄 Primary model failed — switching to fallback: xai via xai",
        )

    def test_short_empty_content_success_line_matches_succeeded_predicate(self):
        # conversation_loop empty-content path uses a shorter wording.
        assert _is_fallback_switch_succeeded_message(
            "lifecycle",
            "↻ Switched to fallback: claude-fable-5 (anthropic)",
        )
        assert _is_fallback_lifecycle_message(
            "lifecycle",
            "↻ Switched to fallback: claude-fable-5 (anthropic)",
        )

    def test_non_lifecycle_kind_never_matches_succeeded(self):
        assert not _is_fallback_switch_succeeded_message(
            "warn", "🔄 Switched to fallback model: a via b → c via d"
        )


class TestRunMetaReannounceOnSuccessOnly:
    """End-to-end-ish: drive `_agent_status_callback`'s decision surface.

    We don't spin up the full streaming machinery (heavy fixture); instead we
    assert the two predicates compose the way `_agent_status_callback` uses
    them, since that callback's control flow is:
        if _is_fallback_lifecycle_message(kind, message):
            put('warning', ...)
            if _is_fallback_switch_succeeded_message(kind, message):
                put('run_meta', ...)
    """

    def test_attempt_then_success_sequence_only_reannounces_once(self):
        events = [
            ("lifecycle", "🔄 Primary model failed — switching to fallback: xai via xai"),
            (
                "lifecycle",
                "🔄 Switched to fallback model: gpt-5.6-sol via openai-codex "
                "→ claude-fable-5 via anthropic",
            ),
        ]
        warnings = 0
        reannounces = 0
        for kind, message in events:
            if _is_fallback_lifecycle_message(kind, message):
                warnings += 1
                if _is_fallback_switch_succeeded_message(kind, message):
                    reannounces += 1
        assert warnings == 2
        assert reannounces == 1
