# Managed worktree garbage collection

Hermes WebUI includes a conservative command-line audit for Git worktrees that
were created for WebUI sessions. The command reads session sidecars directly,
checks process and WebUI activity, then delegates Git-specific classification
and final revalidation to the worktree GC backend.

The default is audit-only. The command does not delete or archive conversations
and does not edit session JSON files.

## Run an audit

Version 1 requires one repository filter:

```bash
python scripts/worktree_gc.py --repo /path/to/repository
```

Useful options include:

```bash
python scripts/worktree_gc.py \
  --repo /path/to/repository \
  --profile default \
  --min-age-days 14 \
  --target-ref origin/master \
  --json
```

`--dry-run` may be supplied explicitly, but it is already the default.
`--collect` is the only switch that enables mutation, and it is mutually
exclusive with `--dry-run`.

Defaults:

- `--profile`: `default`
- `--min-age-days`: `7` (values below `1` are rejected)
- `--target-ref`: `origin/master`
- `--state-dir`: `HERMES_WEBUI_STATE_DIR`, otherwise
  `${HERMES_HOME:-~/.hermes}/webui`
- `--health-url`: `http://127.0.0.1:${HERMES_WEBUI_PORT:-8787}/health`
- `--report-path`:
  `${XDG_STATE_HOME:-~/.local/state}/hermes-webui/worktree-gc/report.json`;
  another user-state path is preferred if needed, and the operating system's
  temporary directory is only the last fallback. Every default stays outside
  the source checkout.

For a scheduled job, pass explicit `--repo`, `--state-dir`, `--health-url`, and
`--report-path` values when the job environment may differ from an interactive
shell. Prevent overlapping invocations at the scheduler level.

## Fail-closed workflow

The audit discovers `<state-dir>/sessions/*.json` without importing the WebUI
runtime or changing the active profile. It ignores `_index.json`, backup names,
and in-progress temporary names. A session without a `profile` field is treated
as `default` for compatibility; an explicit null or different profile is not.
Repository comparison uses canonical path equality, not string prefixes.

A worktree is offered to Git classification only when all of these facts are
confirmed:

1. The session is explicitly archived.
2. Its worktree path, branch, and repository root are valid.
3. Its canonical repository root equals `--repo`.
4. Its age reaches the threshold. `worktree_created_at` is authoritative;
   when it is absent, `updated_at` is the conservative fallback.
5. No active stream is recorded.
6. No pending user text, attachment set, or pending-start timestamp is
   recorded.
7. A complete `/proc` scan finds no process whose canonical cwd equals or is
   below the worktree path.
8. The health endpoint responds successfully with `active_runs == 0`.
9. The Git backend successfully refreshes the repository's default `origin`
   remote once for the complete audit, before the first classification.

`--target-ref` remains the analysis target. The refresh uses `origin` in
version 1 and is not repeated per candidate. If the fetch is absent, fails,
raises, or returns an unverifiable result, collection is globally disabled with
the stable reason `target_refresh_failed`. Command output from Git is not copied
into the report.

Missing or invalid dates, unreadable sidecars, contradictory duplicate
worktree records, an incomplete process scan, a failed health probe, and an
unparseable Git result are uncertainties. Uncertainty keeps the worktree.
Likewise, an active stream, pending submission, process cwd, or WebUI run keeps
the worktree.

The creation-time Hermes worktree lock PID is bookkeeping, not independent
proof of activity. Activity is established by the stream, pending, health, and
cwd checks above.

## Collection

Collection must be requested explicitly:

```bash
python scripts/worktree_gc.py \
  --repo /path/to/repository \
  --report-path /path/to/operator-state/worktree-gc.json \
  --collect
```

The CLI completes the full audit first. Eligible paths are processed from
deepest to shallowest. Immediately before each mutation, the CLI reloads the
session sidecars and repeats the health probe and complete `/proc` cwd scan.
The candidate must still have the same canonical path, branch, repository and
session-ID set; it must still be archived, old enough, and free of stream,
pending-message, pending-attachment, pending-timestamp, and process-cwd
activity.

A candidate-specific race is recorded as
`collection.status="skipped"` with `candidate_runtime_guard`; the Git collector
is not called and the final exit code is `2`. A newly unavailable or active
health endpoint, incomplete scan, or other global runtime uncertainty stops all
remaining mutations. Each remaining item is recorded as skipped with
`global_runtime_guard`, also with exit code `2`. These are safe refusals, not
collector failures.

After that non-Git revalidation, each mutation calls the Git backend with
`dry_run=False`. That backend reclassifies Git immediately before mutation to
close the Git check/use gap without another remote fetch per candidate. If one
real collection fails, no later candidate in that repository is mutated and
the final code is `3`. The partial outcome is still written to the report.

Stale Git worktree metadata is audit-only in version 1. The collector does not
run a repository-wide prune. Patch-equivalent branches are retained, while
ancestor branches can only be deleted through the backend's safe-delete path.

## Report

The JSON report is written through a temporary file in the destination
directory, flushed, then atomically replaced. During collection it is updated
after each revalidation skip and each collection attempt, so both safe refusals
and completed failure paths retain the partial result.

The report contains only operational metadata: session IDs, profile, worktree
identity, normalized timestamps and age, verdicts, reasons, health/process
counters, Git classification, and collection results. It does not include
conversation titles, messages, attachment names or contents, credentials, or
other session content. `--json` prints the same machine-readable report to
stdout; without it, stdout contains a one-line summary and the report path.

## Exit codes

- `0`: audit or collection completed without a blocking anomaly.
- `2`: work in progress, unique/dirty work, activity, or uncertainty was kept.
  This includes a non-Git guard that changed after audit. It is a safe
  no-collection outcome, not permission to bypass a guard.
- `3`: at least one requested collection failed or completed only partially.

Argument errors, such as a missing `--repo`, conflicting mode switches, or an
invalid minimum age, use the command-line parser's standard error exit.

## Prohibited recovery shortcuts

Do not replace the collector with destructive cleanup shortcuts. In
particular, do not use `git clean`, `git reset --hard`, forced branch deletion,
including `git branch -D`, forced worktree removal such as
`git worktree remove --force`, repository-wide worktree pruning, or raw
recursive directory deletion such as `shutil.rmtree`. Do not manually rewrite
session sidecars to make a candidate appear eligible. Resolve the reported
reason, rerun the audit, and let the Git backend revalidate the same
authoritative identity.
