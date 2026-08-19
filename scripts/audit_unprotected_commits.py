#!/usr/bin/env python3
"""Audit local commits that no protected ref covers.

Why this exists
---------------
`/opt/hermes-webui` carries a long stack of site-local commits that are NOT
upstream. The nightly updater (`/usr/local/sbin/hermes-nightly-update`) can
reset or rebase this checkout onto `origin/master`. A local commit survives
that only if it is:

  1. reachable from a ``local/keep-*`` protected ref, AND
  2. declared in the updater's ``WEBUI_PROTECTED_PATCHES`` table with stable
     behavioral markers.

A commit missing either half is lost silently at the next upstream update —
no error, no conflict, just code that quietly disappears.

Fail-closed by design: anything this script cannot positively prove to be
covered is reported as UNPROTECTED. "Unknown" is never "covered".

Scope of the guarantee
----------------------
This audit reports REPLAY protection: whether a commit can be individually
restored by the updater's cherry-pick if it ever leaves the tree. That is the
strong guarantee, and it is the one that matters after a `reset --hard` to
`origin/master` or a rollback.

It is NOT the same question as "will the next nightly update lose my work".
The updater's normal path is an upstream merge into the local stack, which
preserves every commit reachable from HEAD regardless of protection. A commit
flagged here is therefore not necessarily about to disappear tonight — but it
has no individual safety net if the stack is ever reset or rolled back, which
has already happened on this host (see the rollback tombstones in the updater).

Report both facts; do not turn a REPLAY gap into a false "your work is about to
be lost" alarm, and do not use merge-preservation as a reason to skip
protection.

Usage
-----
    scripts/audit_unprotected_commits.py                # audit origin/master..HEAD
    scripts/audit_unprotected_commits.py --rev HEAD     # audit a single commit
    scripts/audit_unprotected_commits.py --quiet        # only print problems

Exit codes: 0 = every audited commit is covered, 1 = at least one is not,
2 = the audit itself could not run (treated as failure by callers).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UPDATER = Path("/usr/local/sbin/hermes-nightly-update")
KEEP_GLOB = "refs/heads/local/keep-*"


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def keep_refs() -> list[str]:
    out = git("for-each-ref", "--format=%(refname:short)", KEEP_GLOB)
    return [line for line in out.splitlines() if line]


def declared_refs() -> set[str] | None:
    """Refs named in the updater's WEBUI_PROTECTED_PATCHES table.

    Returns None when the updater is unreadable: the caller must then treat
    declaration as UNPROVEN rather than assume it is fine.
    """
    try:
        src = UPDATER.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        start = src.index("WEBUI_PROTECTED_PATCHES = (")
        block = src[start : src.index("\nWEBUI_CONFLICT_PREFERRED_OURS")]
    except ValueError:
        return None
    return set(re.findall(r'"ref":\s*"(local/keep-[^"]+)"', block))


def covering_refs(commit: str, refs: list[str]) -> list[str]:
    """Refs that protect `commit`.

    Deliberately uses "points at", NOT "is ancestor of". The updater protects a
    commit by cherry-picking its ref, and a cherry-pick replays exactly the
    commit the ref points at — never its ancestors. Reachability would mark
    every ancestor of a protected tip as covered and make this audit report
    false negatives, which is the exact failure it exists to prevent.
    """
    covering = []
    for ref in refs:
        try:
            if git("rev-parse", f"{ref}^{{commit}}") == commit:
                covering.append(ref)
        except RuntimeError:
            continue
    return covering


def is_merge(commit: str) -> bool:
    """True when `commit` has more than one parent."""
    parents = git("rev-list", "--parents", "-1", commit).split()[1:]
    return len(parents) > 1


def merge_has_unique_resolution(commit: str) -> bool:
    """True when the merge's combined diff is non-empty.

    `git diff-tree --cc` shows only hunks that differ from BOTH parents, i.e.
    content hand-written in the resolution. An empty combined diff means the
    merge is a pure integration of its parents: every line already exists on
    one side (local keep refs or upstream), so there is nothing a replay
    could individually restore.

    NB: `--quiet` is NOT usable here — it suppresses output but exits 0 even
    when the combined diff is non-empty, so it does not discriminate. Use the
    raw `--no-commit-id` output length instead.
    """
    out = git("diff-tree", "--cc", "--no-commit-id", commit)
    return bool(out.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", help="audit this single revision instead of the local stack")
    parser.add_argument("--base", default="origin/master", help="upstream base (default origin/master)")
    parser.add_argument("--quiet", action="store_true", help="print unprotected commits only")
    args = parser.parse_args()

    try:
        refs = keep_refs()
        if args.rev:
            commits = [git("rev-parse", args.rev)]
        else:
            raw = git("rev-list", f"{args.base}..HEAD")
            commits = [c for c in raw.splitlines() if c]
    except RuntimeError as exc:
        print(f"audit could not run: {exc}", file=sys.stderr)
        return 2

    declared = declared_refs()
    if declared is None:
        print(
            f"WARNING: cannot read {UPDATER}; ref declaration is UNPROVEN "
            "(reported as not declared, fail-closed).",
            file=sys.stderr,
        )
        declared = set()

    unprotected: list[tuple[str, str, str]] = []
    for commit in commits:
        subject = git("log", "-1", "--format=%s", commit)[:72]
        covering = covering_refs(commit, refs)
        declared_cover = [r for r in covering if r in declared]
        if declared_cover:
            if not args.quiet:
                print(f"  OK         {commit[:9]}  {subject}")
                print(f"             via {', '.join(declared_cover)}")
        elif covering:
            unprotected.append((commit, subject, f"ref exists but NOT declared in updater: {', '.join(covering)}"))
        elif is_merge(commit):
            if merge_has_unique_resolution(commit):
                unprotected.append((commit, subject, "merge with unique manual resolution (differs from both parents); resolve toward upstream or extract the hunk into its own protected commit"))
            elif not args.quiet:
                print(f"  OK-MERGE   {commit[:9]}  {subject}")
                print("             pure integration merge: combined diff empty, content already covered by a parent")
        else:
            unprotected.append((commit, subject, "no local/keep-* ref covers this commit"))

    print()
    if unprotected:
        print(f"UNPROTECTED: {len(unprotected)} / {len(commits)} commit(s) would be lost on upstream update")
        for commit, subject, reason in unprotected:
            print(f"  ✗ {commit[:9]}  {subject}")
            print(f"             {reason}")
        print()
        print("Fix: create the ref, declare it in WEBUI_PROTECTED_PATCHES with discriminating")
        print("markers, then prove the replay. See the 'hermes-protected-local-patch' skill.")
        return 1

    print(f"All {len(commits)} audited commit(s) are covered by a declared protected ref.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
