"""Regression coverage for profile-scoped sidebar refresh after PWA/BFCache resume.

The user-visible failure was: switching from one Hermes profile to another changed
server-side active profile state, but the installed PWA could keep painting the
old profile's cached conversation list until the app was fully quit/reopened.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def _function_body(src: str, marker: str, next_marker: str | None = None) -> str:
    start = src.index(marker)
    if next_marker is not None:
        end = src.index(next_marker, start)
        return src[start:end]
    depth = 0
    opened = False
    for idx, ch in enumerate(src[start:], start):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Could not extract function body for {marker}")


def test_session_cache_render_refuses_stale_profile_scope():
    body = _function_body(SESSIONS_JS, "function renderSessionListFromCache(){")
    skeleton_guard = body.index("if(_sessionListSkeletonActive) return;")
    scope_guard = body.index("_sessionListScopeMatchesCurrent(_allSessionsScope)")
    purge_idx = body.index("_purgeStaleInflightEntries();")

    assert skeleton_guard < scope_guard < purge_idx, (
        "renderSessionListFromCache must reject a stale profile/scope cache before "
        "doing normal row work, otherwise PWA resume can repaint the old profile's conversations"
    )
    guard_window = body[scope_guard : scope_guard + 220]
    assert "_refreshSessionListAfterScopeMismatch();" in guard_window
    assert "return;" in guard_window


def test_session_scope_helper_compares_profile_and_sidebar_scope():
    helper = _function_body(SESSIONS_JS, "function _sessionListScopeMatchesCurrent(scope){")
    for field in ("profile", "allProfiles", "sidebarSource", "excludeHidden"):
        assert f"scope.{field}" in helper, f"scope guard must compare {field}"
        assert f"current.{field}" in helper, f"scope guard must compare current {field}"


def test_scope_mismatch_forces_fresh_session_fetch_not_cache_repaint():
    helper = _function_body(SESSIONS_JS, "function _refreshSessionListAfterScopeMismatch(){")
    assert "showSessionListSkeleton(S.activeProfile||'default')" in helper
    assert "renderSessionList({deferWhileInteracting:false})" in helper.replace(" ", "")


def test_resume_resync_reads_active_profile_cookie_and_fetches_sessions():
    helper = _function_body(BOOT_JS, "async function _resyncProfileSessionListAfterResume(reason){")
    assert "/api/profile/active" in helper, "resume must re-read the HttpOnly profile cookie via API"
    assert "S.activeProfile = name" in helper, "resume must update browser profile state from server truth"
    assert "renderSessionList({deferWhileInteracting:false})" in helper.replace(" ", ""), (
        "resume must fetch /api/sessions, not just rerender cached rows"
    )
    assert "_showAllProfiles = false" in helper


def test_bfcache_and_installed_pwa_resume_share_same_resync_path():
    assert "window.addEventListener('pageshow'" in BOOT_JS
    assert "document.addEventListener('visibilitychange'" in BOOT_JS
    assert "_restoreWebUiAfterBrowserResume(event)" in BOOT_JS
    assert "_restoreWebUiAfterBrowserResume({type:'visibilitychange'})" in BOOT_JS
    assert "HermesPWA.isStandalone" in BOOT_JS, (
        "visibility resume refresh should be scoped to installed PWA/standalone mode"
    )
