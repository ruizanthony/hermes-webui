"""Cancel provenance must be recorded server-side, not asserted blindly.

Anthony reported seeing "The run was cancelled by the user before <agent>
finished" for a turn he never cancelled (2026-08-17, session a4a41e850c05).

Investigation showed the claim is unfalsifiable in production:

* ``/api/chat/cancel`` is the only path that reaches ``cancel_stream()``.
* ``static/boot.js`` computes a ``reason`` (``composer-stop``, ``slash-stop``,
  ``busy-interrupt``, ``sidebar-stop``, ...) but only writes it to the *browser*
  console — it is never sent to the server.
* This deployment serves the WebUI directly (no reverse proxy), so there is no
  HTTP access log either.

Result: the terminal event is hard-coded to "Cancelled by user" and the UI
renders an accusation the server cannot back up. If some other client, script,
or tab triggers the cancel, the user is still told *they* did it.

These tests pin the contract:

1. ``/api/chat/cancel`` accepts an optional ``reason`` query parameter and
   forwards it to ``cancel_stream()``.
2. The terminal cancel payload carries that provenance so the journal records
   *why* a run ended.
3. An unattributed cancel (no reason supplied) must NOT claim the user did it.
"""

import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class TestCancelEventPayloadProvenance:
    """The terminal payload must carry provenance, not a blanket accusation."""

    def test_payload_accepts_reason_and_exposes_it(self):
        from api.streaming import _cancel_event_payload

        payload = _cancel_event_payload("Cancelled by user", reason="composer-stop")

        assert payload["type"] == "cancelled"
        assert payload["status"] == "cancelled"
        # Provenance must survive into the journal payload.
        assert payload.get("reason") == "composer-stop"

    def test_payload_reason_is_optional_and_omitted_when_unknown(self):
        from api.streaming import _cancel_event_payload

        payload = _cancel_event_payload("Cancelled")

        # No invented provenance when the caller did not supply one.
        assert payload.get("reason") in (None, "")

    def test_unattributed_cancel_message_does_not_blame_the_user(self):
        """A cancel with no attributed origin must not say 'by user'."""
        from api.streaming import _cancel_message_for_reason

        # Explicit user gestures keep the user-facing wording.
        assert "user" in _cancel_message_for_reason("composer-stop").lower()
        assert "user" in _cancel_message_for_reason("slash-stop").lower()
        assert "user" in _cancel_message_for_reason("busy-interrupt").lower()
        assert "user" in _cancel_message_for_reason("sidebar-stop").lower()

        # An unknown / missing origin must stay neutral: the server cannot
        # prove the human did this.
        neutral = _cancel_message_for_reason(None)
        assert "by user" not in neutral.lower()
        assert "cancel" in neutral.lower()


class TestCancelStreamAcceptsReason:
    """cancel_stream() must be able to record why it was called."""

    def test_cancel_stream_signature_accepts_reason(self):
        from api.streaming import cancel_stream

        sig = inspect.signature(cancel_stream)
        assert "reason" in sig.parameters, (
            "cancel_stream() must accept a reason so the terminal event and "
            "server log can record cancel provenance"
        )
        # Must stay backward compatible for existing single-arg callers.
        assert sig.parameters["reason"].default is None


class TestCancelEndpointForwardsReason:
    """The HTTP layer must parse and forward the client-supplied reason."""

    def test_route_reads_reason_query_parameter(self):
        source = _read("api/routes.py")
        idx = source.find('if parsed.path == "/api/chat/cancel":')
        assert idx != -1, "cancel route not found"
        block = source[idx:idx + 4000]

        assert re.search(r'get\(\s*["\']reason["\']', block), (
            "/api/chat/cancel must read the ?reason= query parameter"
        )
        assert "reason=" in block, (
            "/api/chat/cancel must forward the parsed reason to the cancel path"
        )

    def test_server_logs_cancel_provenance(self):
        """An operator must be able to tell who cancelled from server logs."""
        source = _read("api/streaming.py")
        idx = source.find("def cancel_stream(")
        assert idx != -1
        block = source[idx:idx + 6000]

        assert re.search(r"logger\.(info|warning)\(", block), (
            "cancel_stream() must log cancel provenance at info level so a "
            "disputed cancellation can be traced without a reverse proxy log"
        )


class TestFrontendSendsReason:
    """boot.js already computes a reason — it must transmit it."""

    @pytest.mark.parametrize("fn", ["cancelStream", "cancelSessionStream"])
    def test_cancel_requests_include_reason(self, fn):
        source = _read("static/boot.js")
        idx = source.find(f"async function {fn}(")
        assert idx != -1, f"{fn} not found in boot.js"
        block = source[idx:idx + 3000]

        assert "api/chat/cancel" in block
        assert "reason=" in block, (
            f"{fn} must send its computed reason to the server; logging it only "
            "to the browser console leaves the backend unable to attribute the cancel"
        )
