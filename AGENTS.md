# Agent instructions for Hermes WebUI

This file is the shared entry point for AI assistants working in this
repository. Keep it project-specific and safe to publish. Do not put personal
machine setup, private network details, credentials, tokens, or local-only
workflow notes here.

## Read first

Before making changes, read:

1. `README.md`
2. `CONTRIBUTING.md`
3. `docs/CONTRACTS.md`
4. `CHANGELOG.md`

For architecture, testing, or setup work, also read the matching reference:

- `ARCHITECTURE.md` for design constraints and current module layout
- `TESTING.md` for local verification commands and manual test guidance
- `docs/onboarding.md` for first-run onboarding behavior
- `docs/troubleshooting.md` for diagnostic flows
- `docs/rfcs/README.md` for larger RFCs and state/durability contracts

For UI or UX work, read `docs/UIUX-GUIDE.md` and `DESIGN.md` before
changing layout, interaction flow, themes, chat rendering, or composer chrome.

## Onboarding and reinstall support

If the task involves install, reinstall, bootstrap, first-run onboarding,
provider setup, local model server setup, Docker onboarding, WSL onboarding, or
support for a failed first run, read `docs/onboarding-agent-checklist.md`
before running commands or inspecting logs.

Follow that checklist's safety rules:

- use isolated `HERMES_HOME` and `HERMES_WEBUI_STATE_DIR` for trials unless the
  human explicitly asks to use real state
- do not delete or overwrite a real `~/.hermes` directory without explicit
  approval
- do not print API keys, OAuth tokens, cookies, full `.env` files, full
  `auth.json` files, or password hashes
- collect non-secret status and log evidence before recommending a fix

## Local commits must be protected

This checkout carries a long stack of site-local commits that are **not**
upstream. A local commit survives an upstream reset or rollback only when both
halves hold:

1. a `local/keep-*` ref **points at** the commit (not merely contains it — the
   updater's replay is a cherry-pick of the commit its ref points at); and
2. that ref is declared in `WEBUI_PROTECTED_PATCHES` in
   `/usr/local/sbin/hermes-nightly-update`, with markers that are absent before
   the commit and present after it.

Protect the commit **in the session that creates it**. Do not defer it: an
unprotected commit has no individual safety net if the stack is ever reset or
rolled back, which has already happened on this host. Verify before reporting
the work as delivered:

```bash
./scripts/audit_unprotected_commits.py --rev HEAD   # exit 1 => unprotected
./scripts/audit_unprotected_commits.py              # whole local stack
```

A versioned advisory `post-commit` hook surfaces the gap at commit time. It is
not installed by a clone — reinstall it after cloning:

```bash
cp scripts/hooks/post-commit .git/hooks/post-commit && chmod +x .git/hooks/post-commit
```

The hook is deliberately non-blocking: a blocking hook gets bypassed with
`--no-verify`. It makes the omission visible; it does not fix it.

Scope, so the audit is not misread: it measures **replay** protection
(individual restoration after a reset or rollback). The updater's normal path
is an upstream merge, which preserves every commit reachable from `HEAD`. Do
not turn a replay gap into a false "tonight's update will delete your work"
alarm — and do not cite merge-preservation as a reason to skip protection.

## Contribution style

### Final reporting

After implementing, validating, publishing, or deploying a change, keep the
user-facing final report short and outcome-focused. Lead with what was
delivered and its real status, then use at most 3-6 bullets for:

- the main behavior implemented;
- the verified delivery state (local, pushed, PR, deployed, as applicable);
- any remaining blocker or material risk;
- one useful link or exact SHA only when needed for traceability.

Do not reproduce the execution journal. Omit routine commands, exhaustive test
lists, file inventories, branch/worktree details, intermediate failures already
resolved, and repeated evidence unless the user explicitly asks for them or a
failure/risk requires diagnosis. Validation remains thorough; only its final
presentation is compressed.

### Background process completion notifications

Raw background-process completion wakeups (for example, messages beginning
`[IMPORTANT: Background process ... completed ...]`) are internal coordination
signals. The agent currently conducting the conversation owns and consumes
these signals.

- Respond exactly `[[SILENT]]` when the wakeup is routine, successful,
  intermediate, already known, or otherwise requires no user action.
- Do not expose the raw command, process identifier, exit notification, or
  empty output to the user.
- Send a normal user-facing summary only when the wakeup completes the full
  requested task with a useful result, reports a failure or material risk, or
  requires a decision or action from the user.
- Other profiles and worker agents must not independently surface the same
  completion notification.

### Maintenance admission

Autonomous in-process turns (Goal continuations, process wakeups, delegation
completions, and sibling workers) must enter through `start_session_turn` and
therefore `api.maintenance_gate.webui_server_turn_admission`. The gate couples
the Gateway drain marker with the Agent shared maintenance lease and transfers
that lease to the worker until its real completion. A successful `202` must not
create an unleased gap before worker execution. A rejected turn returns
retryable HTTP status `409`; durable producers must release or retain their
claim and must not acknowledge, discard, or consume a retry.
Goal continuations additionally acquire the same gate before claiming an
intent so maintenance cannot create claim churn. Never bypass this chokepoint
for a new autonomous turn source.

### Restarting the live service

The self-update path already refuses to re-exec while chat streams are active
(#1565). Operator-driven restarts — `systemctl restart`, a signal sent to the
service, or a deferred/scheduled restart script — bypass that guarantee and are
the remaining way to kill a live turn.

- Never restart, signal, or re-exec the live service while any session is
  streaming. Poll `/api/sessions?sidebar_source=webui` and require
  `is_streaming == 0`, confirmed on consecutive reads, before acting.
- Fail closed on uncertainty: an unreachable endpoint or an unparsable response
  is NOT idle. A restart helper that runs out of attempts must abort and leave
  the restart to a human; it must never restart anyway after N tries.
- Never restart from a shell whose process tree descends from the service being
  restarted. The restart kills the deploying turn — and any long-running child
  it started — before it can report. Detach the helper (for example via
  `systemd-run --user`) so it outlives the restart it triggers.
- A turn killed mid-write leaves its WebUI sidecar unwritten. The transcript
  survives in `state.db` and is recoverable through
  `_claim_or_synthesize_cli_session`, but the session stays unreachable until
  that sidecar is materialized, so treat an interrupted deploy as unfinished
  until the affected session is verified writeable again.
- A restart that is gated on idleness cannot complete while the requesting
  conversation is itself streaming. Hand the restart to a detached helper and
  end the turn, or restart explicitly and accept that the current turn dies.

- Keep one logical change per PR; split unrelated refactors or cleanup.
- Read `docs/CONTRACTS.md` and the linked contract/RFC for the touched
  subsystem before editing.
- For local pytest runs, use `./scripts/test.sh` instead of bare `python3`,
  `python -m pytest`, or `pytest`. The script creates/uses the repo `.venv`,
  pins execution to Python 3.11-3.13, and installs missing dev test dependencies.
  `HERMES_WEBUI_TEST_PYTHON` selects the supported base interpreter used to
  create or rebuild `.venv`; it must not install test dependencies into a
  system/Homebrew interpreter directly.
  If a direct pytest invocation reports an unsupported interpreter, rerun through
  `./scripts/test.sh` before debugging product code.
- Prefer the existing Python + vanilla JavaScript structure. Do not add
  dependencies, build tools, frameworks, or long-lived processes without clear
  justification and a rollback story.
- Update docs when changing setup, onboarding, runtime behavior, architecture,
  testing guidance, or user-facing workflows.
- Do not edit `CHANGELOG.md` in ordinary contributor PRs. The release workflow
  owns changelog updates through release commits. If a change is release-note
  worthy, include concise release-note wording in the PR body instead.
- For UI or UX changes, include before/after evidence and test relevant
  desktop, narrow, and mobile states.
- For behavior changes, add or update automated tests where practical and list
  the manual verification performed.
- For runtime, streaming, recovery, replay, compression, or sidebar metadata
  changes, name the state layer being mutated and prove the relevant invariant.
- For Docker build changes in `docker_init.bash`, mirror directory exclusions
  in both the `rsync` and `cp -a` paths — `/opt/hermes` may contain subdirectories
  with restricted permissions (e.g. `.playwright/`).

## Before you open a PR — the change guidelines

Read [`docs/GUIDELINES.md`](docs/GUIDELINES.md) in full before non-trivial work. It is the
distilled set of habits that get a change merged in one review round instead of several. The
compressed form:

1. **Fix the class, not the instance.** A bug usually has siblings — other call sites, backends,
   companion endpoints, layouts, exit paths. Find them all and fix the shared chokepoint, or name
   the ones you left out of scope.
2. **Trace one authoritative value end-to-end** (`input → normalize → decision → action → persist →
   cleanup`); the code that *decides* and the code that *acts* must use the same resolved value.
3. **When you can't confirm something, fail closed and say so.** Never take the permissive branch on
   uncertainty; never report a failure as success. "Unknown" is not "allowed."
4. **Enumerate the state-space before editing** — entry point, backend, item count (0/1/many), every
   lifecycle exit (success/error/cancel/replace/teardown), auth on/off, concurrency, hostile input —
   and cover each or mark it out of scope. Most redo rounds are one un-considered dimension.
5. **Assume inputs and check-then-use gaps are adversarial** — validate at the point of use (hold a
   handle, don't re-resolve a path), scope caches by complete identity, handle crafted input.
6. **A test must fail before your fix and pass after it.** Assert observable behavior, not a source
   string or a mock of the thing under test; use multiple items if selection is what's being tested.
7. **Name the owner of every piece of state and prove it's released on every exit** (success, error,
   cancel, replace, shrink, teardown) — not just the happy path.
8. **Fallbacks/defaults are contracts — extend the mechanism, don't copy it.** Editing N parallel
   blocks identically means you missed a chokepoint (e.g. new copy goes in the `en` locale only).
9. **The diff is the task and nothing else.** Extras go in the PR description, not the diff; run the
   affected + neighboring tests before opening.
10. **A visible control costs attention on every visit** — place it by frequency of use and by where
    mainstream chat apps put the equivalent, not by where your diff already is; verify with
    before/after images at desktop and narrow widths.

Show the work in the PR body: the siblings you found, proof the test failed before the fix, the
verification run, before/after images for visible changes, and an explicit list of what you could
not verify.

## Local state and secrets

Hermes WebUI can read and write real agent state, sessions, workspaces,
credentials, and cron data. Treat local validation as potentially destructive
unless you have confirmed the active state directories.

Prefer isolated trial state for experiments:

```bash
HERMES_HOME=/tmp/hermes-webui-agent-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```

Do not include private machine instructions in this tracked file. Use a
git-ignored local note for personal workflow details.
