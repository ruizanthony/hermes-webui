"""Cross-process admission gate for autonomous WebUI turns.

The Gateway drain marker stops browser/network admission, while the Agent
maintenance lock serializes in-process WebUI workers with atomic installation
updates.  Server-owned Goal, process-wakeup, and delegation turns must pass
both checks before claiming or starting work.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from collections.abc import Iterator

logger = logging.getLogger(__name__)


class WebUIMaintenanceInProgress(RuntimeError):
    """Raised when a new autonomous WebUI turn must remain deferred."""


def external_drain_requested() -> bool:
    """Return the authoritative Gateway marker state, failing closed on errors."""
    try:
        from gateway.drain_control import read_drain_request
    except ImportError:
        # Compatibility with Agent versions predating the shared drain marker.
        return False
    try:
        return read_drain_request() is not None
    except Exception as exc:
        logger.warning("WebUI maintenance marker check failed closed: %s", exc)
        return True


@contextmanager
def webui_server_turn_admission() -> Iterator[None]:
    """Hold one shared lease from pre-claim check through turn admission."""
    if external_drain_requested():
        raise WebUIMaintenanceInProgress("Gateway drain is active")

    try:
        from hermes_cli.maintenance_activity import (
            MaintenanceInProgress,
            cli_tui_turn_lease,
        )
    except ImportError:
        # Older Agent installations have no matching exclusive updater lease.
        yield
        return

    try:
        with cli_tui_turn_lease("webui"):
            # Recheck after acquiring the lease so the decision and action use
            # the same protected admission window.
            if external_drain_requested():
                raise WebUIMaintenanceInProgress("Gateway drain is active")
            yield
    except MaintenanceInProgress as exc:
        raise WebUIMaintenanceInProgress(str(exc)) from exc
