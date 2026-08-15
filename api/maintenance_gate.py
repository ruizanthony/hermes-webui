"""Cross-process admission gate for autonomous WebUI turns.

The Gateway drain marker stops browser/network admission, while the Agent
maintenance lock serializes in-process WebUI workers with atomic installation
updates.  Server-owned Goal, process-wakeup, and delegation turns must pass
both checks before claiming or starting work.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
import threading

logger = logging.getLogger(__name__)


class WebUIMaintenanceInProgress(RuntimeError):
    """Raised when a new autonomous WebUI turn must remain deferred."""


class WebUITurnLeaseHandoff:
    """Idempotently transfer an admitted shared lease to a worker thread."""

    def __init__(self, release_callback):
        self._release_callback = release_callback
        self._lock = threading.Lock()
        self._transferred = False
        self._released = False

    @property
    def transferred(self) -> bool:
        with self._lock:
            return self._transferred

    def transfer_to_worker(self) -> None:
        with self._lock:
            if self._released:
                raise RuntimeError("maintenance lease was already released")
            self._transferred = True

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release_callback()


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
def webui_server_turn_admission() -> Iterator[WebUITurnLeaseHandoff]:
    """Hold or transfer one shared lease across the admitted worker's lifetime."""
    if external_drain_requested():
        raise WebUIMaintenanceInProgress("Gateway drain is active")

    try:
        from hermes_cli.maintenance_activity import (
            MaintenanceInProgress,
            cli_tui_turn_lease,
        )
    except ImportError:
        # Older Agent installations have no matching exclusive updater lease.
        handoff = WebUITurnLeaseHandoff(lambda: None)
        try:
            yield handoff
        finally:
            if not handoff.transferred:
                handoff.release()
        return

    lease_context = cli_tui_turn_lease("webui")
    try:
        lease_context.__enter__()
    except MaintenanceInProgress as exc:
        raise WebUIMaintenanceInProgress(str(exc)) from exc

    handoff = WebUITurnLeaseHandoff(
        lambda: lease_context.__exit__(None, None, None)
    )
    try:
        # Recheck after acquiring the lease so the decision and action use
        # the same protected admission window.
        if external_drain_requested():
            raise WebUIMaintenanceInProgress("Gateway drain is active")
        yield handoff
    finally:
        if not handoff.transferred:
            handoff.release()
