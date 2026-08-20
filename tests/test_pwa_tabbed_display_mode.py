"""PWA manifest display-mode chain.

The WebUI is installed as a standalone PWA. Chrome's tabbed application mode
(``display_override: ["tabbed"]``) gives the installed app a real tabbed
document interface, which is what an operator piloting several conversations at
once actually wants — without it they have to leave the installed app and use a
regular browser window just to get tabs.

``display_override`` is a fallback CHAIN: a browser that does not implement a
mode skips to the next one. So declaring ``tabbed`` first is additive — on
Android Chrome (no tabbed-mode support) the chain still resolves to the exact
same ``standalone`` presentation as before. These tests pin that property so the
chain cannot be reordered into something that changes existing behaviour.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MANIFEST_PATH = REPO_ROOT / "static" / "manifest.json"


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This module only reads a static asset; it does not need the HTTP fixture."""


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_is_valid_json_with_a_display_override_chain():
    manifest = _manifest()
    assert isinstance(manifest.get("display_override"), list)
    assert manifest["display_override"], "display_override must not be empty"


def test_tabbed_mode_is_requested_first():
    """Tabbed mode must be the first preference, otherwise a browser that
    supports both tabbed and window-controls-overlay would never select it."""
    override = _manifest()["display_override"]
    assert override[0] == "tabbed"


def test_fallback_chain_still_ends_at_the_previous_presentation():
    """Browsers without tabbed-mode support must land on exactly the presentation
    they used before, so installed apps on Android are unaffected."""
    manifest = _manifest()
    override = manifest["display_override"]
    assert manifest["display"] == "standalone"
    # Everything after 'tabbed' is the pre-existing chain, order preserved.
    assert override[1:] == ["window-controls-overlay", "standalone", "minimal-ui"]
    assert "standalone" in override, (
        "the chain must still resolve to standalone on browsers without "
        "tabbed/window-controls-overlay support"
    )
