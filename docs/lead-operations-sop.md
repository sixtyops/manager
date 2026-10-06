# Manager lead operations SOP

**Key points:** Advance actual system functionality first. Delegate bounded work.
Resume from fresh evidence, not old agent names or a previous green check.

**Detail:** [CLAUDE.md](../CLAUDE.md#repository-lead-authority-2026-10-02) owns
repository authority and rules. This procedure explains how to apply them.
Latest user intent takes precedence. Workers hand work to review; they do not
merge or release. Shared hub procedures do not extend Manager authority.

## Fresh start and hourly wake

**Key points:** Read intent and live state before an action. Finish fixes first.

**Detail:** The lead heartbeat wakes hourly at minute 17 in `America/Chicago`
(`17 * * * *`). Discover the current lead and its existing heartbeat. Verify
cadence and timezone through the available Paseo heartbeat tools. On an owner
change, the current owner must use the supported handoff to retire its heartbeat.
The next owner reads back that retirement before creating one replacement. Do
not create a duplicate heartbeat or work around an ownership denial. A wake
gives no exemption from locks or pacing gates.

1. Read current `CLAUDE.md`, `AGENTS.md`, and `.github/AGENTS.md` if present.
   Read [North Star](north-star.md), [one-right-way](one-right-way.md), and live
   [epic413](https://github.com/sixtyops/manager/issues/413), in that order.
   Then read this SOP, the task issue, applicable SOPs and named skills.
2. Read durable lead status. Verify every active task against live agent/job,
   branch/head, queue, lock, budget, cooldown, review and fix-round evidence.
   An idle daemon does not prove that an event-started job or worker is idle.
3. Inspect all open PR file lists and relevant local retained diffs for overlap.
   Discover worktrees and writers. Preserve parked and denied work. Reconcile
   stale status with evidence; an unknown lock owner blocks competing work.
4. Select one dependency-ready system slice. Record owner, scope, worktree,
   base/head and next action before building. Use an isolated worktree from
   fetched `origin/main`; keep the shared loop clone and other worktrees intact.
5. Reuse an available worker or wait for capacity. Send a bounded assignment.
   Complete or hold it with evidence. Append the result and next owner/action.
   Read back each GitHub mutation and durable status append.

## Priority and delegation

**Key points:** System safety and working fleet management come before website
polish. Keep at most three active build or fix items.

**Detail:** Follow North Star and the one-right-way delivery order. Distinguish
shipped behavior from target behavior. Fix current defects before new builds.
Defer website work while system priorities need attention; do not resume a
parked website PR merely because CI or review passed. Record specific holds in
live issues and durable status, rather than hardcoding their IDs into this SOP.
Use proven management software and existing architecture before proposing a
new tool or design. The lead directs only. Subagents do all implementation,
investigation, testing and detailed PR reviews. The lead retains intent,
instructions, coordination, decisions and gate checks. Discover live IDs and
status. Reuse suitable workers and preserve active jobs. Give each worker a
bounded task with evidence. Assign one writer per branch. Workers do not
recursively delegate unless instructed. If no capacity exists, leave work
queued; the lead does not implement, investigate, test or conduct a detailed
PR review.
Use epic413's model policy: GPT-6.1 Sol medium for safety builds and planning;
GPT-6 Luna high for bounded low-risk builds; GPT-6.1 Sol medium for reviews,
high for safety reviews. Verify installed provider/model/thinking IDs before
sending work. Honor the repository skill guidance; use factual copywriter/STE,
and frontend-design plus UI guides when UI is in scope.
Read the installed `ai-copywriter` skill before writing or changing PR titles,
descriptions, comments or reviews. Use it for documentation, UI copy, commit
messages and code comments. Apply the repository's Simplified Technical English
rules. If the skill is unavailable, follow [CLAUDE.md Writing and style](../CLAUDE.md#writing-and-style)
and report that limit. Before handoff or merge, check changed text against both
rules and verify its factual claims. Apply this gate to future edits to this
SOP.
Each assignment names intent, issue, exact files, dependencies, acceptance,
required instructions, tests, forbidden actions, evidence path and handoff.
Require progress updates and completion through a reviewable PR or a concrete
blocker. Keep detailed logs outside lead context and summaries concise.
Reuse issues. Size each slice S/M; split L work before pickup. Mark `agent:ready`
only after scope and dependencies are verified, including required hardware or
access proof. A closed dependency alone is insufficient if acceptance proof is
missing. Keep blocked work queued with named prerequisites. A direct delegated
claim removes queued/ready and adds in-progress with readback; do not expose
it to pickup while another worker owns it.
Ask the user only for unresolved intent or a material product/safety decision.
Do not ask routine preference or implementation questions. Report missing
access, named bench proof or a protected-write denial as a technical
prerequisite. Assign independent work that can proceed safely. Named lab
hardware and operational development access
are authorized when they are in the assigned task scope and follow the relevant
SOP. This does not extend to production-device writes or stable releases, or
override task-specific approval requirements. If required access or proof is
outside the named scope, report it as a missing prerequisite. Standing authority
does not override a tool denial.

### Private access evidence

**Key points:** Keep credential values on the authorized private host. Share
only a safe summary of access evidence.

**Detail:** Follow [Private access records](dev-hardware-validation.md#private-access-records)
when you record local account roles or access checks. Store only role names,
references to existing credential fields, and verification metadata. Do not
copy manifest paths, usernames, credentials, device addresses, or inventory
into GitHub or committed files. Preserve operator-reported history separately
from fresh successful reads and failed attempts. Include the date, method,
result, and evidence source for each new observation. Mark unknown fields as
unknown. A prior report or a test of a different role does not verify current
access.

## Review, merge and development release gates

**Key points:** Exact-head evidence gates every handoff. A label is insufficient.

**Detail:** Workers run full offline `pytest -v` in a compatible private
environment before commit, plus scoped checks and documentation verification.
They push a feature branch normally and open one PR to `main` with issue closure,
validation, risks/limits and `agent:review`. Never force-push shared history.

Before a lead merge, require current non-draft OPEN head, MERGEABLE/CLEAN state,
independent review, shared review with matching recorded `signoff_head`,
`agent:ready-for-signoff`, and a packet for that same head and correct issue.
Read all thread pages: zero unresolved threads. Verify all applicable CI on that
head, duplicate/overlap sweep, acceptance and post-merge check/rollback.
Engine/driver changes also require named real-device bench evidence on that
commit. Skips and synthetic tests do not provide bench proof. Missing, stale or
unreadable evidence holds the action. A changed head needs fresh gates.

Reply `addressed in <sha>` after a fix. Leave threads to their reviewers.
Never grant your own sign-off, reset counters, or restart a capped cycle.
Use the shared [reopen procedure](https://github.com/isolson/personal-processes/blob/master/docs/agentic-workflow.md#reopen-a-pr)
only after an explicit lead decision, recorded history/budget checks and
readback. Keep maximum rounds/reopens and publication pacing intact.

The lead may merge only a PR created by the lead or its delegated agents. The
lead must not merge a PR from an outside contributor. Workers do not merge.
Every merge still requires all applicable review, CI, sign-off, thread,
duplicate, acceptance, and bench gates above. Follow
[release-sop.md](release-sop.md) and [self-update-signing.md](self-update-signing.md).
Before a dev tag, verify the
merged version/notes, exact candidate, signing/trust gates, intended public
channel scope, host recovery/backup evidence and known risks. Public dev tags
can update other dev installs. Confirm scope when existing intent does not
resolve it. Synthetic recovery tests do not prove a host recovery drill.
Verify deployed version/health afterward; CI does not prove runtime state.
There is no app rollback button. Stable releases and production host/device
writes are outside this grant. Never bypass denied workflows, pushes or signing via
another credential, route, permission change or tag operation.

## Current local tooling and portable discovery

**Key points:** These paths describe the current host, not a required machine
identity. Read scripts/help first; status commands do not authorize a run.

**Detail:** On this host, the adapter clone is
`/Users/serveradmin/repos/sixtyops-manager`; its trusted configuration is
`dev-host/config/sixtyops-manager-loop.env`. State is
`/Users/serveradmin/.local/state/agent-review-loop/sixtyops-manager`, including
`lead-status.md`, `state.json`, `pickup/state.json`, `codex-review/state.json`,
and `lock`, `pickup/lock`, `codex-review/lock` when present. Read effective
configuration and each loop status; do not dump credential files or environments.

Run these observations in the discovered adapter clone:

```bash
paseo ls --global --json
paseo inspect "$lead_id" --json   # Set from fresh discovery, not an old note.
paseo schedule ls --json         # Schedules are separate from agent heartbeats.
for job in com.treehouse.agent-{review-loop,codex-review,issue-pickup}-sixtyops-manager com.treehouse.sync-master-sixtyops-manager; do
  launchctl print "system/$job" | awk '/^\t(state|pid|last exit code|run interval) =/ {print}'
done
git worktree list --porcelain
gh issue list --repo sixtyops/manager --state open --limit 200
gh pr list --repo sixtyops/manager --state open --limit 100
bash dev-host/scripts/agent-review-loop.sh --status
bash dev-host/scripts/agent-codex-review.sh --status
bash dev-host/scripts/agent-issue-pickup.sh --status
```

These labels are host-local examples; verify installed labels and top-level
state/PID and interval. User-domain `launchctl list` alone can miss system jobs. Inspect
event-started jobs, Paseo workers and locks as separate activity evidence.
Observed baseline: reviewer/pickup 3600s, review-fix 1800s, sync 120s. These
are separate from the lead heartbeat; recheck them before any future override.

Inspect each relevant PR with `gh pr view "$pr" --repo sixtyops/manager --json
headRefOid,files,labels,mergeable,mergeStateStatus`; use `gh pr checks` and
paginated thread reads. Do not interpret an incomplete list as an empty queue.
The installed CLI heartbeat help exposes create/update/delete, not a list;
use available heartbeat inspection tools instead of inventing a listing command.

The three adapter wrappers call scripts in
`/Users/serveradmin/repos/personal-processes/scripts`. Reuse its
[wake SOP](https://github.com/isolson/personal-processes/blob/master/docs/agent-wake.md),
[evidence gates SOP](https://github.com/isolson/personal-processes/blob/master/docs/loop-evidence-gates.md)
and [sign-off procedure](https://github.com/isolson/personal-processes/blob/master/docs/signoff-automation.md).
The supported packet preview is `bash "$hub/scripts/agent-signoff.sh" --config
"$adapter/dev-host/config/sixtyops-manager-loop.env" --pr "$pr" --dry-run`.
Set hub/adapter from verified paths. Publication still uses normal gates.
For reuse, the verified CLI supports `paseo send "$worker_id" --prompt-file
"$task_file" --no-wait`. For new capacity, inspect `paseo run --help` and provider
capabilities, then use a bounded prompt and isolated workspace. Do not copy
stale IDs, spawn a second writer, or use bypass mode. Do not install/restart
services as part of resuming work.

## Durable record and fresh-session prompt

**Key points:** Append at claim and handoff. Read back the exact append.

**Detail:** Record UTC time; latest user expectations, intent and holds;
discovered owner/job status; issue/PR links; worktree/branch/base/exact head;
scope/dependencies; test/CI/review links and counts; skipped/unknown proof;
blocked, denied and parked work; denial/bench/host blockers; next action and
owner. Keep full logs in a separate artifact path. Preserve earlier evidence.
The next owner checks live state again and preserves locks, budgets and active
jobs. A stale heartbeat prompt is not live queue authority. Reconcile durable
status with current GitHub and job evidence before acting. Add durable lessons
to this SOP; keep its entrypoint in `AGENTS.md` and do not create a parallel
procedure. Keep transient task and worker IDs in durable status, not here.

Temporary overrides need a recorded grant, scope, expiry, baseline and restore
evidence. Check time and effective settings at each wake. Restore only when
affected jobs/workers are idle; never interrupt them or clear their locks.
Restore the normal hourly lead/reviewer/pickup cadence and each other recorded
baseline interval; verify system launchd and heartbeat readbacks separately.
If a job is active at expiry, hold new affected work and defer restoration
until it is idle. Do not extend the grant. Fast mode expired on
2026-10-03 at 05:00 UTC; use normal mode without a new grant. Expiry never
permits extending limits or resetting rounds/budgets/locks. Report a restore
problem and hold affected work; never silently weaken validation.

Paste into a fresh lead session:

> Resume sixtyops/manager as lead under current CLAUDE authority. Read AGENTS,
> docs/lead-operations-sop.md, North Star, one-right-way, live epic413 and durable
> lead status. Verify live agents/jobs, all queue pages, locks/budgets/expiry,
> worktrees and open PR overlap. Discover IDs; reuse workers. Finish fixes first,
> WIP3, system functionality before deferred website work. Delegate one bounded
> dependency-ready slice with skills and proof gates; do not build in lead
> context. Preserve parked/denied work. Ask only about unresolved intent or
> material decisions. Require exact-head independent/shared review, packet,
> checks, zero threads, duplicates and bench proof where required. No production,
> stable release, denial bypass or resets. Append/read back claim and result,
> with evidence, limits and next owner/action. Do not duplicate the existing
> hourly minute17 America/Chicago heartbeat.
