"""Durable active-turn ownership for terminal session writeback."""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Any


def submitted_prompt_sha256(prompt: str) -> str:
    """Hash the exact submitted prompt after stable text normalization."""
    normalized = unicodedata.normalize("NFC", str(prompt or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_active_checkpoint(*, stream_id: str, turn_id: str, submitted_prompt_text: str) -> dict:
    return {
        "stream_id": str(stream_id or "").strip(),
        "turn_id": str(turn_id or "").strip(),
        "prompt_hash": submitted_prompt_sha256(submitted_prompt_text),
    }


def active_checkpoint_matches(
    session: Any,
    *,
    stream_id: str,
    turn_id: str | None,
    prompt_hash: str | None = None,
    submitted_prompt_text: str | None = None,
) -> bool:
    """Return whether a candidate owns the session's complete checkpoint."""
    active = getattr(session, "active_checkpoint", None)
    if not isinstance(active, dict):
        return False
    candidate_hash = str(prompt_hash or "").strip()
    if not candidate_hash and submitted_prompt_text is not None:
        candidate_hash = submitted_prompt_sha256(submitted_prompt_text)
    candidate = (
        str(stream_id or "").strip(),
        str(turn_id or "").strip(),
        candidate_hash,
    )
    owner = (
        str(active.get("stream_id") or "").strip(),
        str(active.get("turn_id") or "").strip(),
        str(active.get("prompt_hash") or "").strip(),
    )
    active_stream_id = str(getattr(session, "active_stream_id", None) or "").strip()
    return all(candidate) and active_stream_id == candidate[0] and candidate == owner


def clear_active_checkpoint(session: Any) -> None:
    session.active_checkpoint = None
    session.pending_turn_id = None
