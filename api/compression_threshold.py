"""Authoritative compression-threshold resolution for WebUI display.

The agent-side ``ContextCompressor`` applies a small-context threshold floor:
models whose resolved window is below 512K trigger at no less than 75% of the
window even when ``compression.threshold`` (or a per-model override) is lower
(raise-only — an explicitly HIGHER configured value always wins; windows >=
512K keep the configured value).

The WebUI previously had no way to surface that floor: the gauge and the
compression decision could therefore disagree about WHICH threshold applies
(29% displayed against a 75% effective trigger, with the docs/config saying
45-50%). This module resolves the same (configured, effective, floor-applied)
triple from the exact same rules, so:

- the preflight/compression SSE events,
- the session usage payload,
- and the context indicator tooltip

all name the same authoritative numbers instead of implying the configured
value applies.

No agent dependency is required: ``hermes-agent`` is imported lazily so
WebUI-only test environments still load this module.
"""

from __future__ import annotations

# Mirrors hermes-agent agent/context_compressor.py constants. Keep in sync.
SMALL_CTX_WINDOW_LIMIT = 512_000
SMALL_CTX_THRESHOLD_PERCENT = 0.75
DEFAULT_THRESHOLD_PERCENT = 0.50


def _coerce_positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN guard
        return None
    return parsed if parsed > 0 else None


def _resolve_model_threshold(model: str, model_thresholds: dict | None, default: float) -> float:
    """Resolve the per-model override using the agent's own helper when
    available (longest matching substring key wins), with a faithful inline
    fallback when the agent checkout cannot be imported."""
    try:
        from agent.context_compressor import resolve_model_threshold as _agent_fn  # type: ignore

        return float(_agent_fn(model, model_thresholds, default))
    except Exception:
        pass
    if not model_thresholds or not model:
        return default
    best_key = ""
    for key in model_thresholds:
        key_text = str(key)
        if key_text in model and len(key_text) > len(best_key):
            best_key = key_text
    if best_key:
        parsed = _coerce_positive_float(model_thresholds[best_key])
        if parsed is not None:
            return parsed
    return default


def effective_threshold_percent_info(
    *,
    model: str,
    context_length: int | float | None,
    config_data: dict | None = None,
) -> dict:
    """Return ``{configured_percent, effective_percent, floor_applied}``.

    - ``configured_percent``: the value the operator actually configured for
      this model — ``compression.threshold`` with the same per-model
      ``model_thresholds`` override the compressor uses.
    - ``effective_percent``: the value the compressor will ACTUALLY trigger
      at for ``context_length`` — the small-context floor (75% below 512K)
      raises a lower configured value; a higher configured value is kept.
    - ``floor_applied``: True when the floor raised the configured value.
    """
    config_data = config_data if isinstance(config_data, dict) else {}
    compression_cfg = config_data.get("compression")
    if not isinstance(compression_cfg, dict):
        compression_cfg = {}

    raw_threshold = compression_cfg.get("threshold")
    configured = _coerce_positive_float(raw_threshold)
    if configured is None or configured > 1.0:
        configured = DEFAULT_THRESHOLD_PERCENT

    raw_model_thresholds = compression_cfg.get("model_thresholds")
    model_thresholds = (
        {str(k): v for k, v in raw_model_thresholds.items()}
        if isinstance(raw_model_thresholds, dict)
        else None
    )
    configured = _resolve_model_threshold(str(model or ""), model_thresholds, configured)

    # Codex autoraise parity (agent_init): gpt-5.4/5.5 (272K family) and
    # codex-spark raise to a higher model threshold unless explicitly opted
    # out — the raise-only semantics match the compressor exactly. Resolved
    # through the agent's own helper when importable; silently skipped
    # otherwise (WebUI-only test environments).
    try:
        if str(compression_cfg.get("codex_gpt55_autoraise", True)).lower() in ("true", "1", "yes"):
            from agent.auxiliary_client import (  # type: ignore
                _compression_threshold_for_model,
            )

            _model_cthresh = _compression_threshold_for_model(
                str(model or ""), "", allow_codex_gpt55_autoraise=True,
            )
            if _model_cthresh and float(_model_cthresh) > configured:
                configured = float(_model_cthresh)
    except Exception:
        pass

    effective = configured
    floor_applied = False
    window = _coerce_positive_float(context_length)
    if window is not None and window < SMALL_CTX_WINDOW_LIMIT and configured < SMALL_CTX_THRESHOLD_PERCENT:
        effective = SMALL_CTX_THRESHOLD_PERCENT
        floor_applied = True

    return {
        "configured_percent": configured,
        "effective_percent": effective,
        "floor_applied": floor_applied,
    }


def threshold_percent_fields_for_compressor(compressor) -> dict:
    """Extract the same triple from a live compressor instance.

    ``_base_threshold_percent`` is the configured value AFTER the per-model
    override but BEFORE the small-context floor; ``threshold_percent`` is the
    floor-applied value. Older/custom engines lacking either attribute fall
    back to whatever is readable; nothing is fabricated.
    """
    if compressor is None:
        return {}
    configured = getattr(compressor, "_base_threshold_percent", None)
    if configured is None:
        configured = getattr(compressor, "_config_threshold_percent", None)
    if configured is None:
        configured = getattr(compressor, "threshold_percent", None)
    effective = getattr(compressor, "threshold_percent", None)
    fields: dict = {}
    if isinstance(configured, (int, float)) and configured > 0:
        fields["threshold_percent_configured"] = float(configured)
    if isinstance(effective, (int, float)) and effective > 0:
        fields["threshold_percent_effective"] = float(effective)
    if "threshold_percent_configured" in fields and "threshold_percent_effective" in fields:
        fields["threshold_floor_applied"] = bool(
            fields["threshold_percent_effective"] > fields["threshold_percent_configured"] + 1e-9
        )
    return fields


def enrich_usage_fields(usage: dict, fields: dict) -> dict:
    """Merge threshold-percent fields into a usage payload without clobbering
    values that are already present (live compressor beats re-resolution)."""
    if not isinstance(usage, dict):
        return usage
    for key, value in (fields or {}).items():
        if usage.get(key) is None:
            usage[key] = value
    return usage
