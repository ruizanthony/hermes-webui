#!/usr/bin/env python3
"""Audit and optionally collect WebUI-managed Git worktrees."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.worktree_gc_inventory import (  # noqa: E402
    audit_managed_worktrees,
    revalidate_managed_worktree_candidate,
    write_report_atomic,
)


def default_state_dir(environ: dict[str, str] | os._Environ[str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = env.get("HERMES_WEBUI_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    hermes_home = env.get("HERMES_HOME")
    base = Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
    return base / "webui"


def default_health_url(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    env = environ if environ is not None else os.environ
    port = str(env.get("HERMES_WEBUI_PORT") or "8787").strip()
    return f"http://127.0.0.1:{port}/health"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def default_report_path(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    """Prefer durable user state, with temporary storage as the last fallback."""
    env = environ if environ is not None else os.environ
    candidates: list[Path] = []
    xdg_state_home = str(env.get("XDG_STATE_HOME") or "").strip()
    if xdg_state_home:
        xdg_path = Path(xdg_state_home).expanduser()
        if xdg_path.is_absolute():
            candidates.append(
                xdg_path / "hermes-webui" / "worktree-gc" / "report.json"
            )
    candidates.extend(
        (
            Path.home()
            / ".local"
            / "state"
            / "hermes-webui"
            / "worktree-gc"
            / "report.json",
            Path(tempfile.gettempdir())
            / "hermes-webui-worktree-gc"
            / "report.json",
        )
    )
    for candidate in candidates:
        if not _is_within(candidate, REPO_ROOT):
            return candidate
    raise RuntimeError("could not choose a report path outside the repository")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit WebUI-managed worktrees. Collection is disabled unless "
            "--collect is supplied explicitly."
        )
    )
    parser.add_argument("--profile", default="default")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--min-age-days", type=_positive_int, default=7)
    parser.add_argument("--target-ref", default="origin/master")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--health-url", default=default_health_url())
    parser.add_argument("--report-path", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="audit only (the default)",
    )
    mode.add_argument(
        "--collect",
        action="store_true",
        help="collect decisions that pass every guard",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the complete report as JSON to stdout",
    )
    return parser


def load_git_backend() -> Any:
    """Import the separately-owned Git engine only when the CLI actually runs."""
    return importlib.import_module("api.worktree_gc_git")


def _path_depth(value: str) -> int:
    return len(Path(value).resolve(strict=False).parts)


def _collection_succeeded(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is True


_GLOBAL_RUNTIME_REASONS = {
    "active_runs",
    "health_unavailable",
    "process_scan_incomplete",
    "revalidation_failed",
    "session_scan_incomplete",
}
_CANDIDATE_RUNTIME_REASONS = {
    "active_stream",
    "candidate_identity_changed",
    "duplicate_conflict",
    "invalid_or_missing_age",
    "invalid_repo_root",
    "invalid_worktree_branch",
    "invalid_worktree_path",
    "pending_attachments",
    "pending_started_at",
    "pending_user_message",
    "process_cwd_in_worktree",
    "session_not_archived",
    "younger_than_min_age",
}


def _runtime_revalidation_result(
    revalidate_fn: Callable[..., Any],
    **kwargs: Any,
) -> tuple[bool, bool, str | None, list[str]]:
    try:
        result = revalidate_fn(**kwargs)
    except Exception:
        result = None

    allowed = getattr(result, "allowed", None)
    global_guard = getattr(result, "global_guard", None)
    reason = getattr(result, "reason", None)
    if allowed is True and global_guard is False and reason is None:
        return True, False, None, []
    if (
        allowed is False
        and global_guard is True
        and reason == "global_runtime_guard"
    ):
        raw_reasons = getattr(result, "global_reasons", ())
        if not isinstance(raw_reasons, (list, tuple)):
            raw_reasons = ()
        reasons = [
            value
            for value in raw_reasons
            if isinstance(value, str) and value in _GLOBAL_RUNTIME_REASONS
        ]
        return (
            False,
            True,
            "global_runtime_guard",
            reasons or ["revalidation_failed"],
        )
    if (
        allowed is False
        and global_guard is False
        and reason == "candidate_runtime_guard"
    ):
        raw_reasons = getattr(result, "candidate_reasons", ())
        if not isinstance(raw_reasons, (list, tuple)):
            raw_reasons = ()
        reasons = [
            value
            for value in raw_reasons
            if isinstance(value, str) and value in _CANDIDATE_RUNTIME_REASONS
        ]
        if reasons:
            return False, False, "candidate_runtime_guard", reasons
    return False, True, "global_runtime_guard", ["revalidation_failed"]


def _mark_runtime_blocking(report: dict[str, Any]) -> None:
    report["has_blocking_anomalies"] = True
    counts = report.get("counts")
    if isinstance(counts, dict):
        counts["blocking"] = int(counts.get("blocking") or 0) + 1


def _emit_summary(
    report: dict[str, Any],
    report_path: Path,
    *,
    as_json: bool,
    stdout: TextIO,
) -> None:
    if as_json:
        json.dump(
            report,
            stdout,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        stdout.write("\n")
        return
    counts = report.get("counts") or {}
    stdout.write(
        f"mode={report.get('mode')} "
        f"candidates={int(counts.get('candidates') or 0)} "
        f"eligible={int(counts.get('eligible') or 0)} "
        f"blocking={int(counts.get('blocking') or 0)} "
        f"report={report_path}\n"
    )


def _audit_exit_code(report: dict[str, Any]) -> int:
    if not report.get("collection_allowed", False):
        return 2
    if report.get("has_blocking_anomalies", False):
        return 2
    return 0


def _collect(
    report: dict[str, Any],
    *,
    decisions: dict[str, Any],
    git_backend: Any,
    report_path: Path,
    state_dir: Path,
    profile: str,
    repo_filter: Path,
    min_age_days: int,
    health_url: str,
    revalidate_fn: Callable[..., Any],
) -> int:
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        report["collection"] = {
            "attempted": 0,
            "collected": 0,
            "failed": 1,
            "skipped": 0,
        }
        write_report_atomic(report, report_path)
        return 3

    eligible = [
        item
        for item in candidates
        if isinstance(item, dict)
        and item.get("eligible") is True
        and isinstance(item.get("worktree_path"), str)
        and isinstance(item.get("worktree_repo_root"), str)
    ]
    eligible.sort(
        key=lambda item: (
            -_path_depth(item["worktree_path"]),
            item["worktree_path"],
        )
    )
    failed_repos: set[str] = set()
    attempted = 0
    collected = 0
    failed = 0
    skipped = 0

    safety_blocked = False
    for index, item in enumerate(eligible):
        path = item["worktree_path"]
        repo = item["worktree_repo_root"]
        if repo in failed_repos:
            item["collection"] = {
                "status": "skipped",
                "reason": "prior_failure_in_repo",
            }
            skipped += 1
            write_report_atomic(report, report_path)
            continue
        decision = decisions.get(path)
        if decision is None:
            item["collection"] = {
                "status": "failed",
                "reason": "missing_audit_decision",
            }
            failed_repos.add(repo)
            failed += 1
            write_report_atomic(report, report_path)
            continue
        allowed, global_guard, guard_reason, guard_reasons = (
            _runtime_revalidation_result(
                revalidate_fn,
                audited_candidate=item,
                state_dir=state_dir,
                profile=profile,
                repo_filter=repo_filter,
                min_age_days=min_age_days,
                health_url=health_url,
            )
        )
        if not allowed:
            item["collection"] = {
                "status": "skipped",
                "reason": guard_reason,
                "guard_reasons": guard_reasons,
            }
            safety_blocked = True
            skipped += 1
            _mark_runtime_blocking(report)
            if global_guard:
                report["collection_allowed"] = False
                global_reasons = report.get("global_reasons")
                if not isinstance(global_reasons, list):
                    global_reasons = []
                    report["global_reasons"] = global_reasons
                if "global_runtime_guard" not in global_reasons:
                    global_reasons.append("global_runtime_guard")
            write_report_atomic(report, report_path)
            if not global_guard:
                continue
            for remaining in eligible[index + 1 :]:
                if "collection" in remaining:
                    continue
                remaining["collection"] = {
                    "status": "skipped",
                    "reason": "global_runtime_guard",
                    "guard_reasons": guard_reasons,
                }
                skipped += 1
                _mark_runtime_blocking(report)
                write_report_atomic(report, report_path)
            break
        attempted += 1
        try:
            result = git_backend.collect_git_worktree(decision, dry_run=False)
        except Exception as exc:
            result = {
                "ok": False,
                "error_type": type(exc).__name__,
            }
        if _collection_succeeded(result):
            item["collection"] = {
                "status": "collected",
                "result": result,
            }
            collected += 1
        else:
            item["collection"] = {
                "status": "failed",
                "result": result if isinstance(result, dict) else {"ok": False},
            }
            failed_repos.add(repo)
            failed += 1
        write_report_atomic(report, report_path)

    report["collection"] = {
        "attempted": attempted,
        "collected": collected,
        "failed": failed,
        "skipped": skipped,
    }
    write_report_atomic(report, report_path)
    if failed > 0:
        return 3
    if safety_blocked:
        return 2
    return 0


def main(
    argv: list[str] | None = None,
    *,
    git_backend: Any = None,
    audit_fn: Callable[..., dict[str, Any]] = audit_managed_worktrees,
    revalidate_fn: Callable[..., Any] = revalidate_managed_worktree_candidate,
    stdout: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    backend = git_backend if git_backend is not None else load_git_backend()
    report_path = (
        args.report_path.expanduser()
        if args.report_path is not None
        else default_report_path()
    )
    decisions: dict[str, Any] = {}
    report = audit_fn(
        state_dir=args.state_dir,
        profile=args.profile,
        repo_filter=args.repo,
        min_age_days=args.min_age_days,
        target_ref=args.target_ref,
        git_backend=backend,
        health_url=args.health_url,
        decision_sink=decisions,
    )
    report["mode"] = "collect" if args.collect else "dry-run"
    report["collection_requested"] = bool(args.collect)
    if args.collect and not report.get("collection_allowed", False):
        report["collection"] = {
            "attempted": 0,
            "collected": 0,
            "failed": 0,
            "skipped": 0,
            "blocked": True,
            "reasons": list(report.get("global_reasons") or ()),
        }
    write_report_atomic(report, report_path)

    collection_exit_code = 0
    if args.collect and report.get("collection_allowed", False):
        collection_exit_code = _collect(
            report,
            decisions=decisions,
            git_backend=backend,
            report_path=report_path,
            state_dir=args.state_dir,
            profile=args.profile,
            repo_filter=args.repo,
            min_age_days=args.min_age_days,
            health_url=args.health_url,
            revalidate_fn=revalidate_fn,
        )

    _emit_summary(
        report,
        report_path,
        as_json=args.json,
        stdout=output,
    )
    if collection_exit_code:
        return collection_exit_code
    return _audit_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
