"""Regression tests for visible ultra-compact compaction cards."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"function {name} not found")
    brace = source.find("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(source):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def _run_node(script: str) -> None:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_compaction_digest_and_preview_drop_instruction_envelope() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in ("_compactionDigestText", "_compactionCardPreview")
    )
    envelope = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below. This is a handoff from a previous context window."
    )
    digest = (
        '## Historical Task Snapshot\nUser asked: "fix the planning bug"\n'
        "## Goal\nFix the MES planning defect.\n"
        "## Active State\n- worktree ready\n"
        "## Détail complet\nRésumé intégral : lire la note "
        "/opt/obsidian/vault/MES/Hermes/Compactions/compaction-test.md"
    )
    marker = envelope + "\n" + digest + "\n--- END OF CONTEXT SUMMARY ---"
    script = f"""
const assert = require('assert');
{functions}
const marker = {json.dumps(marker)};
const digest = _compactionDigestText(marker);
assert.ok(digest.startsWith('## Historical Task Snapshot'));
assert.ok(!digest.includes('REFERENCE ONLY'));
assert.ok(digest.includes('compaction-test.md'));
const preview = _compactionCardPreview(marker);
assert.ok(preview.includes('fix the planning bug'));
assert.ok(!/REFERENCE ONLY|compacted into the summary/i.test(preview));
assert.ok(preview.length <= 220);
"""
    _run_node(script)


def test_loaded_compaction_marker_scan_finds_every_non_tool_marker() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "_compactionSummarySegment",
            "_isContextCompactionText",
            "_isContextCompactionMessage",
            "_loadedCompactionMarkerRawIdxs",
        )
    )
    marker = "[CONTEXT COMPACTION — REFERENCE ONLY]\n## Goal\nKeep the digest visible."
    messages = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": marker},
        {"role": "tool", "content": marker},
        {"role": "assistant", "content": "normal answer"},
        {"role": "assistant", "content": marker},
    ]
    script = f"""
const assert = require('assert');
const msgContent = (message) => message && typeof message.content === 'string' ? message.content : '';
{functions}
assert.deepStrictEqual(_loadedCompactionMarkerRawIdxs({json.dumps(messages)}), [1, 4]);
"""
    _run_node(script)


def test_latest_agent_compaction_summary_wins_over_stale_webui_anchor() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    helper = "\n".join(
        _extract_function(source, name)
        for name in ("_compactionDigestText", "_resolvedSessionCompressionSummary")
    )
    script = f"""
const assert = require('assert');
{helper}
const session = {{
  compression_anchor_summary: 'old 13KB WebUI snapshot',
  latest_compaction_summary: '[CONTEXT COMPACTION — REFERENCE ONLY] envelope\\n## Goal\\nlatest 5KB Agent digest with Compactions/latest.md',
}};
assert.strictEqual(
  _resolvedSessionCompressionSummary(session),
  '## Goal\\nlatest 5KB Agent digest with Compactions/latest.md'
);
delete session.latest_compaction_summary;
assert.strictEqual(_resolvedSessionCompressionSummary(session), 'old 13KB WebUI snapshot');
"""
    _run_node(script)


def test_settled_reference_is_pinned_at_top_when_marker_is_outside_tail() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    helper = _extract_function(source, "_pinSettledCompressionReferenceAtTop")
    script = f"""
const assert = require('assert');
{helper}
const children = [];
const inner = {{ appendChild(node) {{ children.push(node); return node; }} }};
const card = {{ id: 'compaction-card' }};
assert.strictEqual(_pinSettledCompressionReferenceAtTop(inner, card, -1), true);
assert.deepStrictEqual(children, [card]);
assert.strictEqual(_pinSettledCompressionReferenceAtTop(inner, {{id:'inline'}}, 12), false);
assert.deepStrictEqual(children, [card]);
"""
    _run_node(script)
    assert re.search(
        r"referenceNodePinnedAtTop\s*=\s*_pinSettledCompressionReferenceAtTop\(",
        source,
    ), "render path must pin the settled summary before transcript rows"
    assert "if(!referenceNodePinnedAtTop)" in source


def test_settled_compaction_card_opens_digest_instead_of_collapsed_header() -> None:
    """A truncated tail must show the digest body, not an empty chevron row."""
    source = UI_JS.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    assert "_compressionReferenceCardHtml(referenceText,true)" in compact, (
        "settled pinned card must be open so the digest is visible"
    )
    assert "_compressionReferenceCardHtml(text,false)" in compact, (
        "inline historical marker cards stay collapsed"
    )
    helper = "\n".join(
        _extract_function(source, name)
        for name in (
            "_compactionDigestText",
            "_compactionCardPreview",
            "_compressionReferenceCardHtml",
        )
    )
    script = f"""
const assert = require('assert');
const esc = (value) => String(value);
const t = (key) => key;
const li = () => '';
const _engineAwareCompressionCopy = () => ({{
  label: 'Compaction du contexte',
  preview: 'Référence',
}});
{helper}
const html = _compressionReferenceCardHtml('## Goal\\nKeep the digest visible.', true);
assert.ok(html.includes('tool-card-compress-reference open'), html);
assert.ok(html.includes('## Goal'), html);
assert.ok(html.includes('Keep the digest visible.'), html);
"""
    _run_node(script)


def test_settled_compaction_precedes_load_earlier_chrome() -> None:
    """The digest must be the first top-of-thread control, not Load earlier."""
    source = UI_JS.read_text(encoding="utf-8")
    start = source.find("if(hasServerOlder){")
    assert start != -1, "truncated-tail chrome block not found"
    end = source.find("let lastUserRawIdx", start)
    assert end != -1, "truncated-tail chrome block end not found"
    block = source[start:end]
    pin_at = block.find("_pinSettledCompressionReferenceAtTop")
    load_at = block.find("inner.appendChild(indicator)")
    assert pin_at != -1, "settled card is not pinned in the truncated-tail chrome"
    assert load_at != -1, "load-earlier button is missing from the truncated-tail chrome"
    assert pin_at < load_at, "settled digest must precede Load earlier messages"


def test_collapsed_compaction_preview_is_not_display_none() -> None:
    """Collapsed cards still need a visible preview; global tool-card CSS hides it."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    marker = ".tool-card-compress-reference:not(.open) .tool-card-preview{"
    start = css.find(marker)
    assert start != -1, "collapsed compaction preview rule missing"
    end = css.find("}", start)
    rule = css[start:end + 1]
    assert "display:none" not in rule.replace(" ", "")
    assert re.search(r"display\s*:\s*(inline|block|flex|-webkit-box)", rule)
