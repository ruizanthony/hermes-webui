"""Age-based retention for per-session run journals.

Run journals under ``SESSION_DIR/_run_journal/<session_id>/<run_id>.jsonl``
back live-stream replay and interrupted-turn recovery. They were historically
never pruned, which let the directory grow unbounded (7+ GB observed on
2026-08-17, with single sessions holding hundreds of MB of terminal-state
journals that no recovery path will ever replay again).

Policy:

- A journal FILE is prunable only when its mtime is older than the retention
  window (default 14 days, override via HERMES_WEBUI_RUN_JOURNAL_RETENTION_DAYS,
  0 disables pruning entirely).
- Sessions with a currently ACTIVE run are never touched, whatever the file
  ages — an in-flight worker may still append to or replay any journal of its
  session. This is checked under ACTIVE_RUNS_LOCK.
- Deletion failures are logged and skipped (fail-closed for the file, the
  sweep continues); the sweep never raises to its caller.
- Empty per-session directories are removed after their journals are pruned.

The background worker runs one sweep shortly after startup, then every
``_SWEEP_INTERVAL_SECONDS``. Both the worker and the sweep are synchronous and
cheap: one os.scandir pass, stat calls, and unlink of expired files.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes_webui.run_journal_retention")

_RETENTION_DAYS_DEFAULT = 14
_SWEEP_INTERVAL_SECONDS = 6 * 3600
_STARTUP_DELAY_SECONDS = 180.0

_WORKER_THREAD: threading.Thread | None = None
_WORKER_STOP = threading.Event()
_WORKER_LOCK = threading.Lock()


def _retention_days() -> float:
    raw = os.getenv("HERMES_WEBUI_RUN_JOURNAL_RETENTION_DAYS")
    if raw is None or not str(raw).strip():
        return float(_RETENTION_DAYS_DEFAULT)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid HERMES_WEBUI_RUN_JOURNAL_RETENTION_DAYS=%r; using default %s",
            raw, _RETENTION_DAYS_DEFAULT,
        )
        return float(_RETENTION_DAYS_DEFAULT)
    if value < 0:
        return float(_RETENTION_DAYS_DEFAULT)
    return value


def _journal_root(session_dir: Path | None = None) -> Path:
    from api import run_journal as _rj

    root = session_dir if session_dir is not None else _rj._default_session_dir()
    return Path(root) / _rj.RUN_JOURNAL_DIR_NAME


def _active_session_ids() -> set[str]:
    """Session ids that currently have a registered live/cancelling run."""
    try:
        from api import config as _config

        with _config.ACTIVE_RUNS_LOCK:
            entries = list(_config.ACTIVE_RUNS.values())
    except Exception:
        # Fail closed: if we cannot read the active-run table we must assume
        # every session may be active and prune nothing this sweep.
        logger.warning("run-journal retention: cannot read ACTIVE_RUNS; skipping sweep", exc_info=True)
        return None  # type: ignore[return-value]
    ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            sid = entry.get("session_id")
            if sid:
                ids.add(str(sid))
    return ids


def prune_run_journals(
    *,
    session_dir: Path | None = None,
    max_age_days: float | None = None,
    now: float | None = None,
) -> dict:
    """One retention sweep. Returns counters for observability/tests."""
    stats = {"scanned": 0, "deleted": 0, "kept": 0, "skipped_active": 0,
             "errors": 0, "dirs_removed": 0, "bytes_freed": 0}
    days = _retention_days() if max_age_days is None else float(max_age_days)
    if days <= 0:
        return stats
    cutoff = (time.time() if now is None else float(now)) - days * 86400.0

    root = _journal_root(session_dir)
    if not root.is_dir():
        return stats

    active = _active_session_ids()
    if active is None:
        return stats

    try:
        session_dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        logger.warning("run-journal retention: cannot list %s", root, exc_info=True)
        return stats

    for sdir in session_dirs:
        sid = sdir.name
        if sid in active:
            stats["skipped_active"] += 1
            continue
        remaining = 0
        try:
            entries = list(sdir.iterdir())
        except OSError:
            stats["errors"] += 1
            continue
        for f in entries:
            if not f.is_file() or f.suffix != ".jsonl":
                remaining += 1
                continue
            stats["scanned"] += 1
            try:
                st = f.stat()
            except OSError:
                stats["errors"] += 1
                remaining += 1
                continue
            if st.st_mtime >= cutoff:
                stats["kept"] += 1
                remaining += 1
                continue
            try:
                f.unlink()
                stats["deleted"] += 1
                stats["bytes_freed"] += int(st.st_size)
            except OSError:
                stats["errors"] += 1
                remaining += 1
        if remaining == 0:
            try:
                sdir.rmdir()
                stats["dirs_removed"] += 1
            except OSError:
                # Non-empty (racing writer) or permission issue: leave it.
                pass

    if stats["deleted"] or stats["errors"]:
        logger.info("run-journal retention sweep: %s", stats)
    return stats


def _worker_loop() -> None:
    if _WORKER_STOP.wait(_STARTUP_DELAY_SECONDS):
        return
    while not _WORKER_STOP.is_set():
        try:
            prune_run_journals()
        except Exception:
            logger.exception("run-journal retention sweep failed")
        if _WORKER_STOP.wait(_SWEEP_INTERVAL_SECONDS):
            return


def start_run_journal_retention_worker() -> bool:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return False
        _WORKER_STOP.clear()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            name="webui-run-journal-retention",
            daemon=True,
        )
        try:
            _WORKER_THREAD.start()
        except Exception:
            _WORKER_THREAD = None
            raise
        return True


def stop_run_journal_retention_worker(timeout: float = 2.0) -> bool:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        thread = _WORKER_THREAD
        _WORKER_STOP.set()
        _WORKER_THREAD = None
    if thread is None:
        return False
    thread.join(timeout=timeout)
    return True
