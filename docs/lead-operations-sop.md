# Manager lead operations SOP

**Key points:** Advance actual system functionality first. Delegate bounded work.
Resume from fresh evidence, not old agent names or a previous green check.

**Detail:** [AGENTS.md](../AGENTS.md#agent-rules) owns
repository authority and rules. This procedure explains how to apply them.
Latest user intent takes precedence. The loop limits in AGENTS.md apply to
workers. Operational procedures below require the operator or a lead with
explicit approval for the named action. Historical grants do not override
those limits or task-specific restrictions. See the authority history below.
Workers hand work to review; they do not merge or release. Shared hub
procedures do not extend Manager authority.

## Authority history

**Key points:** The owner approves merges through the ops bot.

**Detail:** The owner delegated lead duties on 2026-10-02. On 2026-10-14, the
owner authorized the Manager lead to merge PRs that met sign-off criteria. See
[Review, merge and development release gates](#review-merge-and-development-release-gates)
for the current process. This grant does not allow push, deploy, release, or
production-device actions.

Public-repo rulesets are free. This supersedes the earlier Team-plan
requirement recorded in [#116](https://github.com/sixtyops/manager/issues/116).

## Fresh start and hourly wake

**Key points:** Read intent and live state before an action. Finish fixes first.

**Detail:** The lead heartbeat wakes hourly at minute 17 in `America/Chicago`
(`17 * * * *`). Discover the current lead and its existing heartbeat. Verify
cadence and timezone through the available Paseo heartbeat tools. On an owner
change, the current owner must use the supported handoff to retire its heartbeat.
The next owner reads back that retirement before creating one replacement. Do
not create a duplicate heartbeat or work around an ownership denial. A wake
gives no exemption from locks or pacing gates.

1. Read current `AGENTS.md` and `.github/AGENTS.md` if present.
   Read [North Star](north-star.md), [one-right-way](one-right-way.md), and live
   [epic413](https://github.com/sixtyops/manager/issues/413), in that order.
   Then read this SOP, the task issue, applicable SOPs and named skills.
2. Read durable lead status. Verify every active task against live agent/job,
   branch/head, queue, lock, budget, cooldown, review and fix-round evidence.
   Before reporting missing lab access, follow [Private access records](dev-hardware-validation.md#private-access-records).
   For dev host access, check the recorded SSH alias and private system record before reporting it missing.
   An idle daemon does not prove that an event-started job or worker is idle.
   Check `paseo permit ls` for pending worker requests at each wake. A request
   may not wake the lead. Allow only a task-authorized action, then read back
   the result. Do not bypass safety review.
   Keep the goal, ordered slices, owners, operator waits, and next action current.
   For each open PR, check its current head, CI, review threads, labels, and
   sign-off packet. Give findings to a worker. Reply `addressed in <sha>` after
   a fix and leave Codex threads for Codex. Run each merged PR's named
   post-merge checks.
3. Inspect all open PR file lists and relevant local retained diffs for overlap.
   Discover worktrees and writers. Preserve parked and denied work. Reconcile
   stale status with evidence; an unknown lock owner blocks competing work.
4. Select one dependency-ready system slice. Record owner, scope, worktree,
   base/head and next action before building. Use an isolated worktree from
   fetched `origin/main`; keep the shared loop clone and other worktrees intact.
5. Follow the review-fix rule in [Priority and delegation](#priority-and-delegation).
   Complete or hold the work with evidence. Append the result and next
   owner/action. Read back each GitHub mutation and durable status append.

## Priority and delegation

**Key points:** System safety and working fleet management come before website
polish. Run parallel workers when their files do not overlap.

**Detail:** Follow North Star and the one-right-way delivery order. Distinguish
shipped behavior from target behavior. Fix current defects before new builds.
Defer website work while system priorities need attention; do not resume a
parked website PR merely because CI or review passed. Record specific holds in
live issues and durable status, rather than hardcoding their IDs into this SOP.
Use proven management software and existing architecture before proposing a
new tool or design. The lead owns the goal, directs the work, and tracks it
through completion or until every remaining step needs an operator. It
delegates implementation, investigation, testing, and detailed PR reviews.
Run as many Luna workers in parallel as have no file overlap. Run workers that
change the same rollout core file one after another: the `updater/app.py` job
runner, `updater/scheduler.py`, `updater/rollout_gate.py`, or
`updater/database.py`. This file-overlap rule replaces the three-active-PR
limit.
The lead does not perform these tasks, even for a small change. It must get
independent review and cannot serve as the designated reviewer for work from
its own team. If no worker capacity exists, leave the work queued.

The shared review-fix loop stays off for Manager under [#599](https://github.com/sixtyops/manager/pull/599),
because its shared script starts only Claude workers ([personal-processes #515](https://github.com/isolson/personal-processes/issues/515)).
Send each review fix to a new Luna worker.

The owner names an agent as lead for a feature, issue, or epic. Every other agent
is a follower: it completes one bounded assignment, reports evidence and any
blocker to its lead, and stops. A follower does not create a wake job or
delegate unless its brief explicitly asks it to. Hold overlapping work if two
agents claim lead ownership, and report the conflict.

When a pre-review, system review or designated review asks for changes, launch
a new fix worker and name the review report in its brief. If the builder has
unpushed work, run the fix worker in the builder's workspace and have it verify
that work first. Reuse a worker only for a follow-up inside its own task, such
as a test rerun.

Use Luna as the default worker for nearly all work, including safety-path work
at high reasoning. Assign docs and small code at low or medium reasoning. Assign
pre-reviews, re-reviews, designated reviews, and bench or dev-host runs at high
reasoning. Use Sol at medium only after Luna fails at a named step, or for a
hard design question named by a reviewer. Use Astra for at most one narrow
design question at medium; never send a whole review to Astra. A PR escalated to
Astra also gets a Sol review. For large decisions, meaning major product or
safety decisions, use the five seats: Astra medium, Sol medium, Luna high, Luna
medium, and the owner. [AGENTS.md](../AGENTS.md#models) points here for model
routing and quorum. Only the Manager lead session may use Claude models.
Do not give implementation work to an architect; give its design to a coding worker.
Verify installed provider/model/thinking IDs before sending work. Follow the
review-fix rule in [Priority and delegation](#priority-and-delegation). Preserve
active jobs. Give each worker a bounded task with evidence. Assign one writer
per branch. Honor
the repository skill guidance; use factual copywriter/STE, and frontend-design
plus UI guides when UI is in scope.
Run delegated Paseo workers in `auto` permission mode with safety review on.
Do not send direct Signal messages from lead or worker sessions. Check the
authorized private lab records and [hardware SOP](dev-hardware-validation.md)
before asking the owner for access already provided. Keep private values out of
the public repository.
Read the installed `ai-copywriter` skill before writing or changing PR titles,
descriptions, comments or reviews. Run its draft and audit loop for every reply
to the owner and every new PR text. Use it for documentation, UI copy, commit
messages and code comments. Apply the repository's Simplified Technical English
rules. If the skill is unavailable, follow [AGENTS.md Writing and style](../AGENTS.md#writing-and-style)
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

### Follower handoff

**Key points:** A follower completes one bounded assignment and reports to its
lead.

**Detail:** Work in the assigned worktree and branch. Run the checks named in
the brief. Do not grow the scope or start a wake job. Ask the lead about an
unclear brief or new risk. Report the PR link, changes, check results, evidence,
and open items. Start the completion message with
`DONE from <id> (<title>):`. Stop when the assignment is done.

### Lead stop condition

**Key points:** Stop only when the goal is complete or all remaining work needs
an operator.

**Detail:** A goal is complete when every planned slice is merged and its
required post-merge checks pass. Do not stop because one PR waits for review if
other safe work can proceed. Mark the goal blocked only when every remaining
step needs an operator and each decision or action is recorded. Before a lead
handoff, update durable lead status and retire or transfer the existing
heartbeat through the supported handoff procedure. Do not create a duplicate.

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
Follow [Slices and reviews](../AGENTS.md#slices-and-reviews) for epic structure,
slice sizing, engine design notes, review scope and findings, reviewer
assignment, base updates, and bench proof ownership.

The lead checks that each eligible non-draft PR awaiting review has
`agent:review`. The trusted-author workflow adds it on open, reopen, or ready
for review and sends a wake.
The reviewer daemon checks every 120 seconds. It allows up to 20 runs per hour
and 200 runs per day, with a 60-second cooldown. These limits apply only to
reviews. Keep the existing locks, generic fix and pickup budgets, and review
round limits. When the existing review process flags a PR for
higher-level review, follow the model policy in [Priority and delegation](#priority-and-delegation).
An independent Luna worker pre-reviews every PR against the system review
criteria before it enters review. The pre-review checks that the PR can pass
on the first round. A final pre-review reports all remaining findings in one
pass. Record the review result and follow the review-fix rule in [Priority and
delegation](#priority-and-delegation).
Hold merge until the review and all normal review and sign-off gates pass. Use
the existing `agent:escalated` and `agent:needs-human` states; do not create a
new review label.

The personal-processes ops bot sends the owner a Signal ready ping or digest
for each Manager PR labeled `agent:ready-for-signoff`, with a ready packet.
The owner approves by replying `merge` on Signal or reacting to the mapped
message. The ops bot merges with `gh pr merge --squash --match-head-commit`.
The lead keeps every gate and uses the 2026-10-14 merge grant only when the
owner asks. The lead sends no Signal messages. After each
ops-bot merge, read the merge commit message on `main`. If it contains a
co-author trailer or a personal name, stop applying `agent:ready-for-signoff`
to PRs and report the finding to the owner. Manager joins the digest through
[personal-processes issue #509](https://github.com/isolson/personal-processes/issues/509)
and [PR #510](https://github.com/isolson/personal-processes/pull/510).

The owner prefers a few larger batches, once or twice a day. Main moves only
when the owner merges. Keep gated PRs ready between batches. After each batch,
one worker updates every open PR from `main` in one pass. Then benches run one
PR at a time, followed by designated approvals. Follow [Squash source checks](#squash-source-checks)
before applying the ready label.

Before a merge batch, have a worker create a scratch worktree detached at the
current `origin/main`. In batch order, squash-apply each queued PR at its
approved head, then run the full offline test suite. The worker pushes nothing
and removes the worktree when the trial ends. Move a PR to the next batch if
it conflicts or introduces a test failure. The trial adds no approval; every
merge gate still applies.

Fetch `origin/main`, then read the current GitHub pull request state and head
before you report a pull request as open or hand it to another owner. Do not
reuse an earlier state after a new event.

Before the owner approves a bot merge, require a current non-draft OPEN head
and MERGEABLE/CLEAN state. Require the designated independent review and shared review with
matching recorded `signoff_head`, `agent:ready-for-signoff`, and a packet for
that head and issue. Read the formal review record. Record its ID, state, full
head SHA, submitted time, and assignment source. The designated review must
have state `APPROVED` on the current head before merge.

A shared-hub `ready-for-signoff` bridge, matching `signoff_head`, label, packet
sentence, or publication account does not by itself prove the designated
independent review. A publication account does not prove reviewer identity.
`COMMENTED` is not `APPROVED`, even when the review body says “approve.” A review
posted after merge is retrospective and cannot meet the pre-merge gate.

If a merge occurs before the review proof is complete, preserve the event times
and evidence. A missing or late review record does not by itself require an
automatic code rollback. The lead delegates code and release-impact review,
evaluates the returned evidence, and records a separate rollback decision. A
later review does not change the pre-merge timeline.

After the review loop passes a head and posts the sign-off packet, keep the
labels unchanged. Review a new head without changing labels; a manual relabel
starts another round and counts toward the round and reopen limits.

At the reopen limit, close the PR and open a replacement from the same branch
and head. Note the replacement in both PRs and leave loop state unchanged. When
the head matches, the designated reviewer posts approval on the replacement PR,
and bench evidence for that exact head still applies.

A denied or failed command may still run. Read live PR, repository, or device
state before retrying a merge or write. Record the observed effect and the
command result separately.

Read all thread pages: zero unresolved threads. Verify all applicable CI on
that head, duplicate/overlap sweep, acceptance and post-merge check/rollback.
Engine/driver changes also require named real-device bench evidence on that
commit. Skips and synthetic tests do not provide bench proof. Missing, stale or
unreadable evidence holds the action. A changed head needs fresh gates.
Start the bench worker with `--env LEAD_AP_ACCESS_FILE=<AP lab file>` and
`--env LEAD_SWITCH_ACCESS_FILE=<switch lab file>`. Use the private paths
recorded under [Private access records](dev-hardware-validation.md#private-access-records),
never paths from the repository. State in the brief that lab access is
authorized and recorded. Use the escalation reason `trusted read-only bench
wrapper, sanitized output only`. In each head checkout, run
`git fetch origin +refs/heads/main:refs/remotes/origin/main` first. Then check
`git merge-base origin/main HEAD`; it must match the current `main` commit.
The wrapper compares against that checkout's `origin/main` and refuses a
valid head when the local reference is stale.

### Squash source checks

**Key points:** Check source commit messages, PR titles, and PR bodies before
the ready label. The ops bot uses GitHub's default squash message and matches
the approved head.

**Detail:** Workers commit as the bot account. In the 2026-10-27 batch,
GitHub's default squash message added a co-author trailer for commits authored
under a personal account; the lead still reads back each merge commit message.
Before the lead applies `agent:ready-for-signoff`, inspect every source
commit message, PR title, and PR body for co-author trailers and personal
names. Ask a worker to remove any such text before the PR enters the sign-off
batch. The ops bot uses
GitHub's default squash message and `--match-head-commit`.

Reply `addressed in <sha>` after a fix. Leave threads to their reviewers.
Never grant your own sign-off, reset counters, or restart a capped cycle.
Use the shared [reopen procedure](https://github.com/isolson/personal-processes/blob/master/docs/agentic-workflow.md#reopen-a-pr)
only after an explicit lead decision, recorded history/budget checks and
readback. Keep maximum rounds/reopens and publication pacing intact.

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

### Dev host runs

Use the same shared lock as CI for each dev host run from the lead host. Keep
one background, non-interactive holder with
`ssh <dev host alias> 'exec flock -w 300 /var/lock/sixtyops-dev-hardware.lock sleep <seconds>'`;
use one background, non-interactive process under a non-root account, and
release the lock at the end.

Before any device write, save and verify the device config snapshot through
Manager so a restore can be proved by comparison.

Run integration tests with `pytest tests/integration -v --tb=no -rN` for the
shared record. Keep full failure output only in a private mode-600 file; have
the worker read it, summarize only sanitized details, and delete it.

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
For a follow-up inside a worker's own task, the verified CLI supports `paseo
send "$worker_id" --prompt-file "$task_file" --no-wait`. For review findings,
launch a new worker with a brief that names the review report. If the builder
has unpushed work, use the builder's workspace and require the new worker to
verify that work first. For new capacity, inspect `paseo run --help` and provider capabilities, then
use a bounded prompt and isolated workspace. `paseo run` ignores `--cwd`; use
`--workspace <id>` or `--new-workspace worktree --worktree-mode branch-off|checkout-pr`.
Pass secrets only through `--env`. Codex workers need escalated sandbox
permission for network access, including `gh` and `git fetch`; state this in
every brief. Keep briefs in the lead state folder, not `/tmp`. Do not
copy stale IDs, spawn a second writer, or use bypass mode. Do not install/restart
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

> Resume sixtyops/manager as lead under current AGENTS authority. Read AGENTS,
> docs/lead-operations-sop.md, North Star, one-right-way, live epic413 and durable
> lead status. Confirm role ownership. Choose models by job within the Manager
> safety policy. Verify live agents/jobs, all queue pages, locks/budgets/expiry,
> worktrees and open PR overlap. Discover IDs. Follow the review-fix rule in
> [Priority and delegation](#priority-and-delegation). Finish fixes first,
> WIP3, system functionality before deferred website work. Delegate bounded
> slices with skills and proof gates. The lead does not implement, investigate,
> test, or review detailed diffs. Require independent review. Preserve
> parked/denied work. Ask only about unresolved
> intent or material decisions. Require exact-head independent/shared review,
> packet, checks, zero threads, duplicates and bench proof where required. No production,
> stable release, denial bypass or resets. Append/read back claim and result,
> with evidence, limits and next owner/action. Do not duplicate the existing
> hourly minute17 America/Chicago heartbeat.
